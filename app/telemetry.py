from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from contextvars import ContextVar
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

CORRELATION_HEADER = "X-Correlation-ID"
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
correlation_id_context: ContextVar[str | None] = ContextVar("correlation_id", default=None)
audit_logger = logging.getLogger("codestra.communication.audit")


def private_otlp_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("OTEL_EXPORTER_OTLP_ENDPOINT must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("OTLP endpoint credentials, query strings, and fragments are forbidden")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        allowed = "." not in hostname or hostname.endswith(
            (".internal", ".local", ".svc", ".cluster.local")
        )
    else:
        allowed = address.is_private or address.is_loopback or address.is_link_local
    if not allowed:
        raise RuntimeError("OTLP endpoint must resolve through an approved private authority")
    return endpoint


def _trace_endpoint(base: str) -> str:
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def configure_telemetry(app: FastAPI) -> bool:
    configured = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not configured:
        return False
    endpoint = private_otlp_endpoint(configured)
    environment = os.getenv("CODESTRA_ENVIRONMENT", "unknown").strip() or "unknown"
    provider = TracerProvider(
        resource=Resource.create(
            {SERVICE_NAME: "codestra-communication", DEPLOYMENT_ENVIRONMENT: environment}
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_trace_endpoint(endpoint)))
    )
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return True


def audit_event(event: str, **fields: object) -> None:
    record = {
        "event": event,
        "service": "codestra-communication",
        "correlation_id": correlation_id_context.get(),
        **fields,
    }
    audit_logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))


def install_correlation_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_boundary(request: Request, call_next):
        supplied = request.headers.get(CORRELATION_HEADER)
        if supplied is not None and not CORRELATION_PATTERN.fullmatch(supplied):
            return JSONResponse(status_code=400, content={"detail": "invalid_correlation_id"})
        correlation_id = supplied or str(uuid4())
        token = correlation_id_context.set(correlation_id)
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("codestra.correlation_id", correlation_id)
        try:
            response = await call_next(request)
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            correlation_id_context.reset(token)
