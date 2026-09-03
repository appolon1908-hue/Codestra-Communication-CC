from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from urllib.parse import urlsplit

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE = "codestra-communication"
_lock = threading.Lock()
_configured = False
_configuration_error: str | None = None


def enabled() -> bool:
    return os.getenv("TELEMETRY_EXPORT_ENABLED", "false").lower() == "true"


def _endpoint() -> str:
    value = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    parsed = urlsplit(value)
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (not parsed.hostname or not (parsed.scheme == "https" or loopback)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("telemetry_endpoint_invalid")
    return value


def _optional_file(name: str, *, private: bool) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("telemetry_file_invalid")
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError("telemetry_file_invalid") from exc
    if not stat.S_ISREG(details.st_mode) or (private and (
        details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077
    )):
        raise ValueError("telemetry_file_invalid")
    return str(path)


def configure() -> tuple[bool, str]:
    global _configured, _configuration_error
    if not enabled():
        return True, "disabled"
    if _configured:
        return True, "ready"
    if _configuration_error:
        return False, _configuration_error
    with _lock:
        if _configured:
            return True, "ready"
        try:
            certificate = _optional_file("OTEL_EXPORTER_OTLP_CERTIFICATE", private=False)
            client_key = _optional_file("OTEL_EXPORTER_OTLP_CLIENT_KEY", private=True)
            client_certificate = _optional_file("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", private=False)
            if (client_key is None) != (client_certificate is None):
                raise ValueError("telemetry_mtls_incomplete")
            provider = TracerProvider(resource=Resource.create({
                "service.name": SERVICE,
                "service.version": os.getenv("CODESTRA_RELEASE_VERSION", "unknown"),
                "deployment.environment.name": os.getenv("CODESTRA_ENVIRONMENT", "unknown"),
            }))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=_endpoint(), certificate_file=certificate,
                client_key_file=client_key, client_certificate_file=client_certificate,
            )))
            trace.set_tracer_provider(provider)
            _configured = True
        except Exception:
            _configuration_error = "telemetry_configuration_invalid"
            return False, _configuration_error
    return True, "ready"


def tracer():
    configure()
    return trace.get_tracer(SERVICE)


def extract(carrier: dict[str, str]):
    return propagate.extract(carrier)


def inject(carrier: dict[str, str]) -> None:
    propagate.inject(carrier)


def current_trace_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    inject(carrier)
    return {key: value for key, value in carrier.items() if key in {"traceparent", "tracestate"}}


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None
