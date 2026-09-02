import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    recipient: Mapped[str] = mapped_column(String(512))
    template_key: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(32), default="marketing")
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(48), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    resource_version: Mapped[int] = mapped_column(Integer, default=1)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_message_idempotency"),
        UniqueConstraint("tenant_id", "id", name="uq_message_tenant_id"),
    )


class MessageEventModel(Base):
    __tablename__ = "communication_message_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    previous_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    new_status: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(160), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    safe_detail: Mapped[str | None] = mapped_column(String(240), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE", name="fk_communication_event_message",
        ),
    )


class CommunicationEventOutboxModel(Base):
    __tablename__ = "communication_event_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    topic: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id"], ["communication_message_events.id"],
            ondelete="CASCADE", name="fk_communication_event_outbox_event",
        ),
    )


class MessageMutationModel(Base):
    __tablename__ = "communication_message_mutations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mutation_type: Mapped[str] = mapped_column(String(48), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE", name="fk_communication_mutation_message",
        ),
        UniqueConstraint(
            "tenant_id", "message_id", "mutation_type", "idempotency_key",
            name="uq_communication_message_mutation",
        ),
    )


class CommunicationAuditModel(Base):
    __tablename__ = "communication_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(160), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsentModel(Base):
    __tablename__ = "communication_consents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    subject_key: Mapped[str] = mapped_column(String(256), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, default="legacy")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "subject_key", "channel", name="uq_consent_subject_channel"),
    )


class PreferenceModel(Base):
    __tablename__ = "communication_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    topic: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    consent: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False, default="unspecified")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "subject", "channel", "topic", name="uq_communication_preference"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_preference_idempotency"),
    )


class SuppressionModel(Base):
    __tablename__ = "communication_suppressions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(512))
    reason: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, default="legacy")
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "recipient", name="uq_suppression_recipient"),
    )


class TemplateModel(Base):
    __tablename__ = "communication_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    locale: Mapped[str] = mapped_column(String(24), nullable=False, default="en")
    subject_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "locale", name="uq_communication_template_key_locale"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_template_idempotency"),
    )


class SendingDomainModel(Base):
    __tablename__ = "communication_sending_domains"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="dns_required")
    spf: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    dkim: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    dmarc: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    reverse_dns: Mapped[str] = mapped_column(String(24), nullable=False, default="not_configured")
    tls: Mapped[str] = mapped_column(String(24), nullable=False, default="not_configured")
    bimi: Mapped[str] = mapped_column(String(24), nullable=False, default="not_configured")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", name="uq_communication_sending_domain"),
        UniqueConstraint("tenant_id", "id", name="uq_communication_domain_tenant_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_domain_idempotency"),
    )


class SenderIdentityModel(Base):
    __tablename__ = "communication_sender_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    domain_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "domain_id"], ["communication_sending_domains.tenant_id", "communication_sending_domains.id"],
            ondelete="RESTRICT", name="fk_communication_sender_domain",
        ),
        UniqueConstraint("tenant_id", "channel", "address", name="uq_communication_sender_identity"),
        UniqueConstraint("tenant_id", "id", name="uq_communication_sender_tenant_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_sender_idempotency"),
    )


class DomainMutationModel(Base):
    __tablename__ = "communication_domain_mutations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_key: Mapped[str] = mapped_column(String(256), nullable=False)
    mutation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "mutation_type", "idempotency_key", name="uq_communication_domain_mutation"),
    )


class CommunicationOperationModel(Base):
    __tablename__ = "communication_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="deliver")
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    middleware_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE", name="fk_communication_operation_message",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_operation_idempotency"),
        UniqueConstraint("tenant_id", "id", name="uq_communication_operation_tenant_id"),
    )


class DeliveryOutboxModel(Base):
    __tablename__ = "communication_delivery_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["communication_operations.tenant_id", "communication_operations.id"],
            ondelete="CASCADE", name="fk_communication_outbox_operation",
        ),
    )


class ProviderInboxModel(Base):
    __tablename__ = "communication_provider_inbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="processed")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"], ["messages.tenant_id", "messages.id"],
            ondelete="CASCADE", name="fk_communication_provider_inbox_message",
        ),
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_communication_provider_event"
        ),
    )
