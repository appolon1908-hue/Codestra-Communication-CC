from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


class MiddlewareDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class MiddlewareResult:
    operation_id: str
    state: str


class MiddlewareCommunicationClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("MIDDLEWARE_BASE_URL", "").rstrip("/")
        self.token_file = os.getenv("MIDDLEWARE_TOKEN_FILE", "")
        self.timeout = max(1.0, min(float(os.getenv("MIDDLEWARE_TIMEOUT_SECONDS", "10")), 60.0))

    def _origin(self) -> str:
        parsed = urlsplit(self.base_url)
        loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            not parsed.hostname
            or not (parsed.scheme == "https" or loopback)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MiddlewareDeliveryError("middleware_base_url_invalid", retryable=False)
        return self.base_url

    def _token(self) -> str:
        path = Path(self.token_file)
        if not self.token_file or not path.is_file() or path.is_symlink():
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise MiddlewareDeliveryError("middleware_token_empty", retryable=False)
        return token

    async def dispatch(self, payload: dict[str, Any]) -> MiddlewareResult:
        action = str(payload.get("action", "deliver"))
        if action == "cancel":
            path = f"/api/v1/operations/{quote(str(payload['delivery_operation_id']), safe='')}/cancel"
        else:
            channel = str(payload.get("channel", ""))
            if channel not in {"email", "sms"}:
                raise MiddlewareDeliveryError("middleware_channel_not_supported", retryable=False)
            path = f"/api/v1/control/communications/{channel}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Tenant-ID": str(payload["tenant_id"]),
            "X-Correlation-ID": str(payload["correlation_id"]),
            "Idempotency-Key": str(payload["operation_id"]),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self._origin()}{path}", headers=headers, json=payload)
        except httpx.TransportError as exc:
            raise MiddlewareDeliveryError(
                "middleware_outcome_unknown", retryable=True, outcome_unknown=True
            ) from exc
        if response.status_code not in {200, 202}:
            transient = response.status_code in {408, 425, 429} or response.status_code >= 500
            raise MiddlewareDeliveryError(
                f"middleware_rejected_{response.status_code}", retryable=transient,
                outcome_unknown=response.status_code >= 500,
            )
        try:
            document = response.json()
            operation_id = str(document["operation_id"]).strip()
            state = str(document["state"]).strip()
            if not operation_id or len(operation_id) > 128 or not state or len(state) > 32:
                raise ValueError("response identity outside storage bounds")
        except (KeyError, TypeError, ValueError) as exc:
            raise MiddlewareDeliveryError(
                "middleware_response_invalid", retryable=True, outcome_unknown=True
            ) from exc
        return MiddlewareResult(operation_id, state)
