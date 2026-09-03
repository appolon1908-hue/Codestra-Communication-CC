from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


HTTP_REQUESTS = Counter(
    "codestra_communication_http_requests_total",
    "HTTP requests completed by status class.",
    ("method", "status_class"),
)
HTTP_DURATION = Histogram(
    "codestra_communication_http_request_duration_seconds",
    "HTTP request duration by method.",
    ("method",),
)
DELIVERY_OUTBOX = Gauge(
    "codestra_communication_delivery_outbox_records",
    "Durable delivery outbox records by bounded state.",
    ("state",),
)
OPERATIONS = Gauge(
    "codestra_communication_operations",
    "Communication operations by bounded state.",
    ("state",),
)
PROVIDER_INBOX = Gauge(
    "codestra_communication_provider_inbox_records",
    "Persisted provider inbox records by bounded state.",
    ("state",),
)
EVENT_OUTBOX_DEPTH = Gauge(
    "codestra_communication_event_outbox_depth",
    "Unpublished durable integration events.",
)
EVENT_PUBLICATIONS = Counter(
    "codestra_communication_event_publications_total",
    "Integration event publication outcomes.",
    ("outcome",),
)
MIDDLEWARE_REQUESTS = Counter(
    "codestra_communication_middleware_requests_total",
    "Middleware command outcomes using bounded labels.",
    ("outcome",),
)
MIDDLEWARE_CIRCUIT_OPEN = Gauge(
    "codestra_communication_middleware_circuit_open",
    "Whether the Middleware dependency circuit is currently open.",
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
