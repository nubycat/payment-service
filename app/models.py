from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import (
    OperationStatus,
    ReceiptDisposition,
    ReceiptResult,
    SubmissionIntentStatus,
)

_OPERATION_STATUS_TYPE = SAEnum(
    OperationStatus,
    name="operation_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
)
_INTENT_STATUS_TYPE = SAEnum(
    SubmissionIntentStatus,
    name="submission_intent_status",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
)
_RECEIPT_RESULT_TYPE = SAEnum(
    ReceiptResult,
    name="receipt_result",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
)
_RECEIPT_DISPOSITION_TYPE = SAEnum(
    ReceiptDisposition,
    name="receipt_disposition",
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=32,
)


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(operation_id)) > 0",
            name="ck_operations_operation_id_not_blank",
        ),
        CheckConstraint(
            "amount > 0 AND scale(amount) BETWEEN 0 AND 2",
            name="ck_operations_amount_positive_scale",
        ),
        CheckConstraint("currency = 'RUB'", name="ck_operations_currency_rub"),
        CheckConstraint(
            "status IN ('CREATED', 'PROCESSING', 'COMPLETED', 'REJECTED')",
            name="ck_operations_status",
        ),
        CheckConstraint(
            "provider_payment_id IS NULL OR char_length(btrim(provider_payment_id)) > 0",
            name="ck_operations_provider_payment_id_not_blank",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_operations_timestamps"),
        UniqueConstraint(
            "provider_payment_id",
            name="uq_operations_provider_payment_id",
        ),
        Index(
            "ix_operations_processing",
            "updated_at",
            "operation_id",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[OperationStatus] = mapped_column(
        _OPERATION_STATUS_TYPE,
        nullable=False,
        server_default=text("'CREATED'"),
    )
    provider_payment_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    submission_intent: Mapped[SubmissionIntent | None] = relationship(
        back_populates="operation",
        passive_deletes=True,
        uselist=False,
    )
    events: Mapped[list[OperationEvent]] = relationship(
        back_populates="operation",
        order_by="OperationEvent.event_id",
        passive_deletes=True,
    )
    receipts: Mapped[list[Receipt]] = relationship(
        back_populates="operation",
        order_by="Receipt.id",
        passive_deletes=True,
    )


class SubmissionIntent(Base):
    __tablename__ = "submission_intents"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_submission_intents_operation_id"),
        CheckConstraint("id > 0", name="ck_submission_intents_id_positive"),
        CheckConstraint(
            "idempotency_key = operation_id",
            name="ck_submission_intents_idempotency_key",
        ),
        CheckConstraint(
            "correlation_id = operation_id",
            name="ck_submission_intents_correlation_id",
        ),
        CheckConstraint(
            "status IN "
            "('PENDING', 'IN_FLIGHT', 'RETRY_WAIT', 'ACCEPTED', 'RESOLVED', 'BLOCKED')",
            name="ck_submission_intents_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_submission_intents_attempt_count",
        ),
        CheckConstraint(
            "last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599",
            name="ck_submission_intents_http_status",
        ),
        CheckConstraint(
            "jsonb_typeof(request_payload) = 'object' "
            "AND request_payload - ARRAY['operationId', 'amount', 'currency'] = '{}'::jsonb "
            "AND request_payload ?& ARRAY['operationId', 'amount', 'currency'] "
            "AND request_payload ->> 'operationId' = operation_id "
            "AND request_payload ->> 'currency' = 'RUB'",
            name="ck_submission_intents_payload",
        ),
        CheckConstraint(
            "(status IN ('PENDING', 'RETRY_WAIT') "
            "AND next_attempt_at IS NOT NULL AND lease_until IS NULL) "
            "OR (status = 'IN_FLIGHT' "
            "AND next_attempt_at IS NULL AND lease_until IS NOT NULL) "
            "OR (status IN ('ACCEPTED', 'RESOLVED', 'BLOCKED') "
            "AND next_attempt_at IS NULL AND lease_until IS NULL)",
            name="ck_submission_intents_schedule_state",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_submission_intents_timestamps",
        ),
        Index(
            "ix_submission_intents_ready",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('PENDING', 'RETRY_WAIT')"),
        ),
        Index(
            "ix_submission_intents_expired_lease",
            "lease_until",
            "id",
            postgresql_where=text("status = 'IN_FLIGHT'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("operations.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[SubmissionIntentStatus] = mapped_column(
        _INTENT_STATUS_TYPE,
        nullable=False,
        server_default=text("'PENDING'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_http_status: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    operation: Mapped[Operation] = relationship(back_populates="submission_intent")


class OperationEvent(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        CheckConstraint("event_id >= 1", name="ck_operation_events_event_id_positive"),
        CheckConstraint(
            "(type = 'CREATED' AND event_id = 1 "
            "AND from_status IS NULL AND to_status = 'CREATED') "
            "OR (type = 'PROCESSING' AND event_id > 1 "
            "AND from_status = 'CREATED' AND to_status = 'PROCESSING') "
            "OR (type = 'COMPLETED' AND event_id > 1 "
            "AND from_status = 'PROCESSING' AND to_status = 'COMPLETED') "
            "OR (type = 'REJECTED' AND event_id > 1 "
            "AND from_status = 'PROCESSING' AND to_status = 'REJECTED')",
            name="ck_operation_events_transition",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="ck_operation_events_metadata_object",
        ),
        UniqueConstraint(
            "operation_id",
            "type",
            name="uq_operation_events_operation_type",
        ),
        Index(
            "uq_operation_events_one_final",
            "operation_id",
            unique=True,
            postgresql_where=text("type IN ('COMPLETED', 'REJECTED')"),
        ),
    )

    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("operations.operation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    event_type: Mapped[OperationStatus] = mapped_column(
        "type",
        _OPERATION_STATUS_TYPE,
        nullable=False,
    )
    from_status: Mapped[OperationStatus | None] = mapped_column(_OPERATION_STATUS_TYPE)
    to_status: Mapped[OperationStatus] = mapped_column(
        _OPERATION_STATUS_TYPE,
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    operation: Mapped[Operation] = relationship(back_populates="events")


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_receipts_fingerprint"),
        CheckConstraint("id > 0", name="ck_receipts_id_positive"),
        CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_receipts_fingerprint_sha256",
        ),
        CheckConstraint(
            "char_length(btrim(provider_payment_id)) > 0",
            name="ck_receipts_provider_payment_id_not_blank",
        ),
        CheckConstraint(
            "result IN ('COMPLETED', 'REJECTED')",
            name="ck_receipts_result",
        ),
        CheckConstraint(
            "disposition IN "
            "('APPLIED', 'IGNORED_ALREADY_FINAL', 'IGNORED_CONFLICTING_RESULT', "
            "'REJECTED_PROVIDER_ID_CONFLICT')",
            name="ck_receipts_disposition",
        ),
        CheckConstraint(
            "delivery_count >= 1",
            name="ck_receipts_delivery_count",
        ),
        CheckConstraint(
            "last_received_at >= received_at",
            name="ck_receipts_received_timestamps",
        ),
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object' "
            "AND raw_payload - ARRAY["
            "'operationId', 'providerPaymentId', 'result', 'message', 'occurredAt'] = '{}'::jsonb "
            "AND raw_payload ?& ARRAY["
            "'operationId', 'providerPaymentId', 'result', 'message', 'occurredAt'] "
            "AND raw_payload ->> 'operationId' = operation_id "
            "AND raw_payload ->> 'providerPaymentId' = provider_payment_id "
            "AND raw_payload ->> 'result' = result "
            "AND raw_payload ->> 'message' = message",
            name="ck_receipts_raw_payload",
        ),
        Index(
            "ix_receipts_operation_received",
            "operation_id",
            "received_at",
            "id",
        ),
        Index("ix_receipts_provider_payment_id", "provider_payment_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("operations.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[ReceiptResult] = mapped_column(
        _RECEIPT_RESULT_TYPE,
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    provider_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    delivery_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    disposition: Mapped[ReceiptDisposition] = mapped_column(
        _RECEIPT_DISPOSITION_TYPE,
        nullable=False,
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    operation: Mapped[Operation] = relationship(back_populates="receipts")
