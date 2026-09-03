from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .metrics import MIDDLEWARE_CIRCUIT_OPEN, MIDDLEWARE_REQUESTS


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
        self.failure_threshold = max(
            1, min(int(os.getenv("MIDDLEWARE_CIRCUIT_FAILURE_THRESHOLD", "5")), 20)
        )
        self.recovery_seconds = max(
            1.0, min(float(os.getenv("MIDDLEWARE_CIRCUIT_RECOVERY_SECONDS", "30")), 300.0)
        )
        self._consecutive_failures = 0
        self._open_until = 0.0

    def _before_request(self) -> None:
        if self._open_until > time.monotonic():
            MIDDLEWARE_CIRCUIT_OPEN.set(1)
            MIDDLEWARE_REQUESTS.labels(outcome="circuit_open").inc()
            raise MiddlewareDeliveryError("middleware_circuit_open", retryable=True)
        MIDDLEWARE_CIRCUIT_OPEN.set(0)

    def _success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0
        MIDDLEWARE_CIRCUIT_OPEN.set(0)

    def _failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open_until = time.monotonic() + self.recovery_seconds
            MIDDLEWARE_CIRCUIT_OPEN.set(1)

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
        if not self.token_file or not path.is_absolute():
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False) from exc
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(8193)
        finally:
            os.close(fd)
        if len(raw) > 8192:
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False)
        try:
            token = raw.decode("utf-8", "strict").strip()
        except UnicodeDecodeError as exc:
            raise MiddlewareDeliveryError("middleware_token_file_invalid", retryable=False) from exc
        if not token:
            raise MiddlewareDeliveryError("middleware_token_empty", retryable=False)
        return token

    async def dispatch(self, payload: dict[str, Any]) -> MiddlewareResult:
        self._before_request()
        action = str(payload.get("action", "deliver"))
        if action == "reconcile":
            path = f"/api/v1/operations/{quote(str(payload['middleware_operation_id']), safe='')}"
            method = "GET"
        elif action == "cancel":
            path = f"/api/v1/operations/{quote(str(payload['delivery_operation_id']), safe='')}/cancel"
            method = "POST"
        else:
            channel = str(payload.get("channel", ""))
            if channel not in {"email", "sms"}:
                raise MiddlewareDeliveryError("middleware_channel_not_supported", retryable=False)
            path = f"/api/v1/control/communications/{channel}"
            method = "POST"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Tenant-ID": str(payload["tenant_id"]),
            "X-Correlation-ID": str(payload["correlation_id"]),
            "Idempotency-Key": str(payload["operation_id"]),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self._origin()}{path}",
                    headers=headers,
                    json=payload if method == "POST" else None,
                )
        except httpx.TransportError as exc:
            self._failure()
            MIDDLEWARE_REQUESTS.labels(outcome="transport_error").inc()
            raise MiddlewareDeliveryError(
                "middleware_outcome_unknown", retryable=True, outcome_unknown=True
            ) from exc
        if response.status_code not in {200, 202}:
            transient = response.status_code in {408, 425, 429} or response.status_code >= 500
            if transient:
                self._failure()
            else:
                self._success()
            MIDDLEWARE_REQUESTS.labels(outcome="rejected").inc()
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
            self._failure()
            MIDDLEWARE_REQUESTS.labels(outcome="invalid_response").inc()
            raise MiddlewareDeliveryError(
                "middleware_response_invalid", retryable=True, outcome_unknown=True
            ) from exc
        self._success()
        MIDDLEWARE_REQUESTS.labels(outcome="accepted").inc()
        return MiddlewareResult(operation_id, state)
