#!/usr/bin/env python3
"""Apply the reviewed Communication production-remediation changes once.

This script is intentionally exact-string based: any source drift fails closed instead
of silently applying a partial remediation.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"PATCH_DRIFT={path.relative_to(ROOT)} expected=1 actual={count}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def append_new(path: Path, content: str) -> None:
    if path.exists():
        raise SystemExit(f"PATCH_DRIFT={path.relative_to(ROOT)} already_exists")
    path.write_text(content, encoding="utf-8")


main = ROOT / "app/main.py"
auth = ROOT / "app/auth.py"
delivery = ROOT / "app/delivery_worker.py"
telemetry = ROOT / "app/telemetry.py"

replace_once(
    auth,
    '                "communications.operations.reconcile": "Reconcile uncertain communication operations",\n'
    '                "metrics.read": "Read private service metrics",\n',
    '                "communications.operations.reconcile": "Reconcile uncertain communication operations",\n'
    '                "communications.preferences.read": "Read recipient communication preferences",\n'
    '                "communications.preferences.write": "Manage recipient communication preferences",\n'
    '                "communications.domains.read": "Read sending-domain state",\n'
    '                "communications.domains.write": "Manage and verify sending domains",\n'
    '                "communications.senders.read": "Read sender identities",\n'
    '                "communications.senders.write": "Manage sender identities",\n'
    '                "communications.usage.read": "Read tenant-scoped communication usage",\n'
    '                "metrics.read": "Read private service metrics",\n',
)

replace_once(
    main,
    'class Channel(StrEnum):\n'
    '    EMAIL = "email"\n'
    '    SMS = "sms"\n'
    '    WHATSAPP = "whatsapp"\n'
    '    PUSH = "push"\n\n\n'
    'class Purpose(StrEnum):\n',
    'class Channel(StrEnum):\n'
    '    EMAIL = "email"\n'
    '    SMS = "sms"\n'
    '    WHATSAPP = "whatsapp"\n'
    '    PUSH = "push"\n\n\n'
    'DELIVERY_CHANNELS = frozenset({Channel.EMAIL, Channel.SMS})\n\n\n'
    'class Purpose(StrEnum):\n',
)

replace_once(
    main,
    'def _protected_match(hash_column, legacy_column, value_hash: str, legacy_value: str):\n'
    '    if os.getenv("COMMUNICATION_ALLOW_LEGACY_PLAINTEXT_READS", "false").lower() == "true":\n'
    '        return or_(hash_column == value_hash, legacy_column == legacy_value)\n'
    '    return hash_column == value_hash\n\n\n'
    'def _actor(request: Request | None) -> str:\n',
    'def _protected_match(hash_column, legacy_column, value_hash: str, legacy_value: str):\n'
    '    if os.getenv("COMMUNICATION_ALLOW_LEGACY_PLAINTEXT_READS", "false").lower() == "true":\n'
    '        return or_(hash_column == value_hash, legacy_column == legacy_value)\n'
    '    return hash_column == value_hash\n\n\n'
    'def _recipient_policy_lock_key(tenant_id: str, channel: str, recipient_hash: str) -> int:\n'
    '    digest = hashlib.sha256(\n'
    '        b"codestra-communication-policy\\x00"\n'
    '        + tenant_id.encode("utf-8")\n'
    '        + b"\\x00"\n'
    '        + channel.encode("utf-8")\n'
    '        + b"\\x00"\n'
    '        + recipient_hash.encode("ascii")\n'
    '    ).digest()[:8]\n'
    '    return int.from_bytes(digest, "big", signed=True)\n\n\n'
    'async def _lock_recipient_policy(\n'
    '    session: AsyncSession, tenant_id: str, channel: str, recipient_hash: str\n'
    ') -> None:\n'
    '    await session.execute(\n'
    '        select(func.pg_advisory_xact_lock(\n'
    '            _recipient_policy_lock_key(tenant_id, channel, recipient_hash)\n'
    '        ))\n'
    '    )\n\n\n'
    'async def _require_current_replay_result(\n'
    '    session: AsyncSession, *, tenant_id: str, kind: str,\n'
    '    idempotency_key: str, current_version: int,\n'
    ') -> None:\n'
    '    prior = await session.scalar(\n'
    '        select(DomainMutationModel).where(\n'
    '            DomainMutationModel.tenant_id == tenant_id,\n'
    '            DomainMutationModel.mutation_type == kind,\n'
    '            DomainMutationModel.idempotency_key == idempotency_key,\n'
    '        )\n'
    '    )\n'
    '    if prior is None:\n'
    '        raise HTTPException(status_code=409, detail="idempotency_replay_missing")\n'
    '    if prior.result_version != current_version:\n'
    '        raise HTTPException(status_code=409, detail="idempotency_result_superseded")\n\n\n'
    'def _actor(request: Request | None) -> str:\n',
)

replace_once(
    main,
    '        "email": True,\n'
    '        "sms": True,\n'
    '        "whatsapp": True,\n'
    '        "push": True,\n',
    '        "email": True,\n'
    '        "sms": True,\n'
    '        "whatsapp": False,\n'
    '        "push": False,\n'
    '        "delivery_channels": sorted(channel.value for channel in DELIVERY_CHANNELS),\n',
)

replace_once(
    main,
    '    recipient = _recipient(body.recipient, body.channel)\n'
    '    if not recipient:\n'
    '        raise HTTPException(status_code=400, detail="recipient_invalid")\n'
    '    fingerprint = _fingerprint(tenant_id, body, recipient)\n',
    '    recipient = _recipient(body.recipient, body.channel)\n'
    '    if not recipient:\n'
    '        raise HTTPException(status_code=400, detail="recipient_invalid")\n'
    '    if EXTERNAL_DELIVERY_ENABLED and body.channel not in DELIVERY_CHANNELS:\n'
    '        raise HTTPException(status_code=422, detail="channel_delivery_not_supported")\n'
    '    fingerprint = _fingerprint(tenant_id, body, recipient)\n',
)

replace_once(
    main,
    '    consent_hash = _protect_value(\n'
    '        recipient, tenant_id=tenant_id, purpose="consent-subject"\n'
    '    )[1]\n\n'
    '    existing = await session.execute(\n',
    '    consent_hash = _protect_value(\n'
    '        recipient, tenant_id=tenant_id, purpose="consent-subject"\n'
    '    )[1]\n'
    '    await _lock_recipient_policy(\n'
    '        session, tenant_id, body.channel.value, suppression_hash\n'
    '    )\n\n'
    '    existing = await session.execute(\n',
)

replace_once(
    main,
    '    recipient_ciphertext, recipient_hash = _protect_value(\n'
    '        recipient, tenant_id=x_tenant_id, purpose="suppression-recipient"\n'
    '    )\n'
    '    row = await session.scalar(\n'
    '        select(SuppressionModel)\n',
    '    recipient_ciphertext, recipient_hash = _protect_value(\n'
    '        recipient, tenant_id=x_tenant_id, purpose="suppression-recipient"\n'
    '    )\n'
    '    await _lock_recipient_policy(\n'
    '        session, x_tenant_id, body.channel.value, recipient_hash\n'
    '    )\n'
    '    row = await session.scalar(\n'
    '        select(SuppressionModel)\n',
)

replace_once(
    main,
    '    ):\n'
    '        assert row is not None\n'
    '        return _suppression_response(row)\n'
    '    if row is None:\n'
    '        row = SuppressionModel(\n',
    '    ):\n'
    '        assert row is not None\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="suppression.create",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _suppression_response(row)\n'
    '    if row is None:\n'
    '        row = SuppressionModel(\n',
)

replace_once(
    main,
    '    row = await session.scalar(\n'
    '        select(SuppressionModel)\n'
    '        .where(SuppressionModel.id == suppression_id, SuppressionModel.tenant_id == x_tenant_id)\n'
    '        .with_for_update()\n'
    '    )\n'
    '    if row is None:\n'
    '        raise HTTPException(status_code=404, detail="suppression_not_found")\n'
    '    payload = {"expected_version": expected_version}\n',
    '    snapshot = await session.scalar(\n'
    '        select(SuppressionModel).where(\n'
    '            SuppressionModel.id == suppression_id,\n'
    '            SuppressionModel.tenant_id == x_tenant_id,\n'
    '        )\n'
    '    )\n'
    '    if snapshot is None:\n'
    '        raise HTTPException(status_code=404, detail="suppression_not_found")\n'
    '    recipient = _reveal_value(\n'
    '        snapshot.recipient_ciphertext, snapshot.recipient,\n'
    '        tenant_id=snapshot.tenant_id, purpose="suppression-recipient",\n'
    '    )\n'
    '    recipient_hash = _protect_value(\n'
    '        recipient, tenant_id=x_tenant_id, purpose="suppression-recipient"\n'
    '    )[1]\n'
    '    await _lock_recipient_policy(\n'
    '        session, x_tenant_id, snapshot.channel, recipient_hash\n'
    '    )\n'
    '    row = await session.scalar(\n'
    '        select(SuppressionModel)\n'
    '        .where(SuppressionModel.id == suppression_id, SuppressionModel.tenant_id == x_tenant_id)\n'
    '        .with_for_update()\n'
    '    )\n'
    '    if row is None:\n'
    '        raise HTTPException(status_code=404, detail="suppression_not_found")\n'
    '    payload = {"expected_version": expected_version}\n',
)

replace_once(
    main,
    '    ):\n'
    '        return _suppression_response(row)\n'
    '    if row.resource_version != expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.active = False\n',
    '    ):\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="suppression.delete",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _suppression_response(row)\n'
    '    if row.resource_version != expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.active = False\n',
)

replace_once(
    main,
    '    ):\n'
    '        assert row is not None\n'
    '        return _consent_response(row)\n'
    '    if row is None:\n'
    '        row = ConsentModel(\n',
    '    ):\n'
    '        assert row is not None\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="consent.grant",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _consent_response(row)\n'
    '    if row is None:\n'
    '        row = ConsentModel(\n',
)

replace_once(
    main,
    '    ):\n'
    '        return _consent_response(row)\n'
    '    if body.expected_version is None or row.resource_version != body.expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.status = "revoked"\n',
    '    ):\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="consent.revoke",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _consent_response(row)\n'
    '    if body.expected_version is None or row.resource_version != body.expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.status = "revoked"\n',
)

replace_once(
    main,
    '        if row is None:\n'
    '            raise HTTPException(status_code=409, detail="preference_replay_missing")\n'
    '        return _preference_response(row)\n'
    '    if row is None:\n'
    '        if body.expected_version is not None:\n',
    '        if row is None:\n'
    '            raise HTTPException(status_code=409, detail="preference_replay_missing")\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="preference.upsert",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _preference_response(row)\n'
    '    if row is None:\n'
    '        if body.expected_version is not None:\n',
)

replace_once(
    main,
    '    ):\n'
    '        return _sender_response(row)\n'
    '    if body.expected_version is None or row.resource_version != body.expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.channel = body.channel.value\n',
    '    ):\n'
    '        await _require_current_replay_result(\n'
    '            session, tenant_id=x_tenant_id, kind="sender.update",\n'
    '            idempotency_key=idempotency_key, current_version=row.resource_version,\n'
    '        )\n'
    '        return _sender_response(row)\n'
    '    if body.expected_version is None or row.resource_version != body.expected_version:\n'
    '        raise HTTPException(status_code=409, detail="stale_resource_version")\n'
    '    row.channel = body.channel.value\n',
)

replace_once(
    main,
    'def provider_statuses() -> list[ProviderStatus]:\n'
    '    state = "middleware_routed" if EXTERNAL_DELIVERY_ENABLED else "delivery_disabled"\n'
    '    health = "not_probed" if EXTERNAL_DELIVERY_ENABLED else "disabled"\n'
    '    return [\n'
    '        ProviderStatus(\n'
    '            channel=channel,\n'
    '            route="middleware",\n'
    '            state=state,\n'
    '            health=health,\n'
    '            reputation="not_applicable" if not EXTERNAL_DELIVERY_ENABLED else "not_probed",\n'
    '        )\n'
    '        for channel in Channel\n'
    '    ]\n',
    'def provider_statuses() -> list[ProviderStatus]:\n'
    '    result: list[ProviderStatus] = []\n'
    '    for channel in Channel:\n'
    '        if not EXTERNAL_DELIVERY_ENABLED:\n'
    '            result.append(ProviderStatus(\n'
    '                channel=channel, route="middleware", state="delivery_disabled",\n'
    '                health="disabled", reputation="not_applicable",\n'
    '            ))\n'
    '        elif channel in DELIVERY_CHANNELS:\n'
    '            result.append(ProviderStatus(\n'
    '                channel=channel, route="middleware", state="middleware_routed",\n'
    '                health="not_probed", reputation="not_probed",\n'
    '            ))\n'
    '        else:\n'
    '            result.append(ProviderStatus(\n'
    '                channel=channel, route="none", state="unsupported",\n'
    '                health="not_applicable", reputation="not_applicable",\n'
    '            ))\n'
    '    return result\n',
)

replace_once(
    main,
    'def provider_health() -> ProviderHealth:\n'
    '    status = "disabled" if not EXTERNAL_DELIVERY_ENABLED else "degraded"\n'
    '    reason = "external_delivery_disabled" if not EXTERNAL_DELIVERY_ENABLED else "runtime_probe_not_configured"\n'
    '    return ProviderHealth(\n'
    '        status=status,\n'
    '        checked_at=datetime.now(timezone.utc),\n'
    '        providers=[\n'
    '            ProviderHealthItem(provider="middleware", channel=channel, status=status, reason=reason)\n'
    '            for channel in Channel\n'
    '        ],\n'
    '    )\n',
    'def provider_health() -> ProviderHealth:\n'
    '    if not EXTERNAL_DELIVERY_ENABLED:\n'
    '        return ProviderHealth(\n'
    '            status="disabled", checked_at=datetime.now(timezone.utc),\n'
    '            providers=[\n'
    '                ProviderHealthItem(\n'
    '                    provider="middleware", channel=channel, status="disabled",\n'
    '                    reason="external_delivery_disabled",\n'
    '                )\n'
    '                for channel in Channel\n'
    '            ],\n'
    '        )\n'
    '    return ProviderHealth(\n'
    '        status="degraded", checked_at=datetime.now(timezone.utc),\n'
    '        providers=[\n'
    '            ProviderHealthItem(\n'
    '                provider="middleware" if channel in DELIVERY_CHANNELS else "none",\n'
    '                channel=channel,\n'
    '                status="degraded" if channel in DELIVERY_CHANNELS else "unsupported",\n'
    '                reason=(\n'
    '                    "runtime_probe_not_configured"\n'
    '                    if channel in DELIVERY_CHANNELS\n'
    '                    else "channel_delivery_not_supported"\n'
    '                ),\n'
    '            )\n'
    '            for channel in Channel\n'
    '        ],\n'
    '    )\n',
)

replace_once(
    delivery,
    '        except DataProtectionError:\n'
    '            row.state = "dead_letter"\n'
    '            row.lease_until = None\n'
    '            row.last_error_code = "data_protection_unavailable"\n'
    '            operation.state = "failed"\n'
    '            operation.error_code = "data_protection_unavailable"\n'
    '            await session.commit()\n'
    '            return None\n',
    '        except DataProtectionError:\n'
    '            row.state = "dead_letter"\n'
    '            row.lease_until = None\n'
    '            row.last_error_code = "data_protection_unavailable"\n'
    '            operation.state = "failed"\n'
    '            operation.error_code = "data_protection_unavailable"\n'
    '            message = await session.scalar(\n'
    '                select(MessageModel)\n'
    '                .where(\n'
    '                    MessageModel.id == operation.message_id,\n'
    '                    MessageModel.tenant_id == operation.tenant_id,\n'
    '                )\n'
    '                .with_for_update()\n'
    '            )\n'
    '            if message is not None:\n'
    '                previous = message.status\n'
    '                message.status = "delivery_failed"\n'
    '                if previous != message.status:\n'
    '                    message.resource_version += 1\n'
    '                event_type = "communication.message.delivery_failed"\n'
    '                await record_message_event(\n'
    '                    session, message, event_type=event_type, previous_status=previous,\n'
    '                    actor_id="communication-delivery-worker",\n'
    '                    correlation_id=operation.correlation_id,\n'
    '                    safe_detail="data_protection_unavailable",\n'
    '                )\n'
    '                session.add(CommunicationAuditModel(\n'
    '                    tenant_id=message.tenant_id, aggregate_type="message",\n'
    '                    aggregate_id=message.id, action=event_type, outcome="failed",\n'
    '                    actor_id="communication-delivery-worker",\n'
    '                    correlation_id=operation.correlation_id,\n'
    '                ))\n'
    '            await session.commit()\n'
    '            return None\n',
)

telemetry.write_text('''from __future__ import annotations

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


def _trusted_parent_chain(path: Path) -> None:
    current = path.parent
    while True:
        try:
            details = current.lstat()
        except OSError as exc:
            raise ValueError("telemetry_file_invalid") from exc
        if current.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise ValueError("telemetry_file_invalid")
        if details.st_uid not in {0, os.geteuid()} or stat.S_IMODE(details.st_mode) & 0o022:
            raise ValueError("telemetry_file_invalid")
        if current.parent == current:
            return
        current = current.parent


def _optional_file(name: str, *, private: bool) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("telemetry_file_invalid")
    _trusted_parent_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("telemetry_file_invalid") from exc
    try:
        details = os.fstat(descriptor)
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("telemetry_file_invalid")
        if details.st_uid not in {0, os.geteuid()} or mode & 0o022:
            raise ValueError("telemetry_file_invalid")
        if private and (details.st_uid != os.geteuid() or mode & 0o077):
            raise ValueError("telemetry_file_invalid")
        if details.st_size <= 0:
            raise ValueError("telemetry_file_invalid")
        identity = (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ValueError("telemetry_file_invalid") from exc
    if path.is_symlink() or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
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
''', encoding="utf-8")

append_new(
    ROOT / "tests/test_production_remediation_v2.py",
    '''from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import main
from app.auth import service_bearer
from app.main import Channel, MessageCreate, Purpose, create_message, provider_statuses
from app.telemetry import _optional_file


def test_all_enforced_oauth_scopes_are_declared() -> None:
    scopes = service_bearer.model.flows.clientCredentials.scopes
    required = {
        "communications.preferences.read", "communications.preferences.write",
        "communications.domains.read", "communications.domains.write",
        "communications.senders.read", "communications.senders.write",
        "communications.usage.read",
    }
    assert required.issubset(scopes)


def test_provider_routing_is_channel_specific_when_delivery_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    by_channel = {item.channel: item for item in provider_statuses()}
    assert by_channel[Channel.EMAIL].state == "middleware_routed"
    assert by_channel[Channel.SMS].state == "middleware_routed"
    assert by_channel[Channel.WHATSAPP].state == "unsupported"
    assert by_channel[Channel.PUSH].state == "unsupported"
    assert by_channel[Channel.WHATSAPP].route == "none"


@pytest.mark.asyncio
async def test_unsupported_delivery_channel_is_rejected_before_database_work(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    session = AsyncMock()
    with pytest.raises(HTTPException) as denied:
        await create_message(
            MessageCreate(
                channel=Channel.WHATSAPP, recipient="+1 555 010 1000",
                template_key="unsupported.test", purpose=Purpose.TRANSACTIONAL,
            ),
            "tenant-test", "unsupported-channel-key", session,
        )
    assert denied.value.status_code == 422
    assert denied.value.detail == "channel_delivery_not_supported"
    session.execute.assert_not_awaited()


def test_private_telemetry_file_uses_same_descriptor_and_trusted_parent(monkeypatch) -> None:
    directory = Path.cwd() / f".telemetry-test-{uuid.uuid4()}"
    directory.mkdir(mode=0o700)
    path = directory / "client.key"
    path.write_bytes(b"synthetic-private-key")
    path.chmod(0o600)
    try:
        monkeypatch.setenv("TEST_TELEMETRY_KEY", str(path))
        assert _optional_file("TEST_TELEMETRY_KEY", private=True) == str(path)
    finally:
        shutil.rmtree(directory)


def test_world_writable_telemetry_parent_is_rejected(monkeypatch, tmp_path) -> None:
    path = tmp_path / "client.key"
    path.write_bytes(b"synthetic-private-key")
    path.chmod(0o600)
    monkeypatch.setenv("TEST_TELEMETRY_KEY", str(path))
    with pytest.raises(ValueError, match="telemetry_file_invalid"):
        _optional_file("TEST_TELEMETRY_KEY", private=True)
''',
)

append_new(
    ROOT / "tests/test_production_remediation_postgres.py",
    '''from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.data_protection import blind_index
from app.delivery_worker import run_once as run_delivery_once
from app.main import (
    Channel, MessageCreate, PreferenceWrite, Purpose, SuppressionCreate,
    create_message, create_suppression, upsert_preference,
)
from app.models import (
    CommunicationAuditModel, CommunicationOperationModel, DeliveryOutboxModel,
    MessageEventModel, MessageModel,
)

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_suppression_activation_serializes_message_admission(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-policy-{uuid.uuid4()}"
    recipient = f"policy-{uuid.uuid4()}@example.invalid"
    entered = asyncio.Event()
    release = asyncio.Event()
    original = main._record_domain_mutation

    async def paused(*args, **kwargs):
        if kwargs.get("kind") == "suppression.create":
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(main, "_record_domain_mutation", paused)

    async def activate():
        async with sessions() as session:
            return await create_suppression(
                SuppressionCreate(
                    recipient=recipient, channel=Channel.EMAIL, reason="policy-test"
                ),
                tenant, f"suppression-{uuid.uuid4()}", session,
            )

    async def admit():
        async with sessions() as session:
            return await create_message(
                MessageCreate(
                    channel=Channel.EMAIL, recipient=recipient,
                    template_key="policy.test", purpose=Purpose.TRANSACTIONAL,
                ),
                tenant, f"message-{uuid.uuid4()}", session,
            )

    suppression_task = asyncio.create_task(activate())
    await asyncio.wait_for(entered.wait(), timeout=2)
    message_task = asyncio.create_task(admit())
    await asyncio.sleep(0.1)
    assert not message_task.done()
    release.set()
    suppression = await asyncio.wait_for(suppression_task, timeout=3)
    assert suppression.active is True
    with pytest.raises(HTTPException) as blocked:
        await asyncio.wait_for(message_task, timeout=3)
    assert blocked.value.detail == "recipient_suppressed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_preference_replay_rejects_superseded_result(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-replay-{uuid.uuid4()}"
    subject = f"replay-{uuid.uuid4()}@example.invalid"
    original_key = f"preference-{uuid.uuid4()}"
    original_body = PreferenceWrite(
        subject=subject, channel=Channel.EMAIL, consent="granted", source="test"
    )
    async with sessions() as session:
        created = await upsert_preference(original_body, tenant, original_key, session)
    async with sessions() as session:
        updated = await upsert_preference(
            PreferenceWrite(
                subject=subject, channel=Channel.EMAIL, consent="denied",
                source="test", expected_version=created.resource_version,
            ),
            tenant, f"preference-{uuid.uuid4()}", session,
        )
        assert updated.resource_version == created.resource_version + 1
    async with sessions() as session:
        with pytest.raises(HTTPException) as superseded:
            await upsert_preference(original_body, tenant, original_key, session)
        assert superseded.value.detail == "idempotency_result_superseded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pre_dispatch_data_protection_failure_updates_public_message(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    monkeypatch.setenv("EXTERNAL_DELIVERY_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-payload-{uuid.uuid4()}"
    async with sessions() as session:
        message = await create_message(
            MessageCreate(
                channel=Channel.EMAIL,
                recipient=f"payload-{uuid.uuid4()}@example.invalid",
                template_key="payload.test", purpose=Purpose.TRANSACTIONAL,
            ),
            tenant, f"message-{uuid.uuid4()}", session,
        )
        message_id = message.id
        operation_id = message.operation_id
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(
                DeliveryOutboxModel.operation_id == operation_id
            )
        )
        outbox.payload_json = "v1:invalid-protected-payload"
        await session.commit()

    class NeverCalled:
        async def dispatch(self, _payload):
            raise AssertionError("dispatch must not run after payload decryption failure")

    assert not await run_delivery_once(
        NeverCalled(), lease_seconds=30, max_attempts=3, session_factory=sessions
    )
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        operation = await session.get(CommunicationOperationModel, operation_id)
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(
                DeliveryOutboxModel.operation_id == operation_id
            )
        )
        assert message.status == "delivery_failed"
        assert operation.state == "failed"
        assert outbox.state == "dead_letter"
        assert await session.scalar(select(MessageEventModel.id).where(
            MessageEventModel.message_id == message_id,
            MessageEventModel.event_type == "communication.message.delivery_failed",
        )) is not None
        assert await session.scalar(select(CommunicationAuditModel.id).where(
            CommunicationAuditModel.aggregate_id == message_id,
            CommunicationAuditModel.action == "communication.message.delivery_failed",
        )) is not None
    await engine.dispose()
''',
)

print("COMMUNICATION_PRODUCTION_REMEDIATION_V2=APPLIED")
