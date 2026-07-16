"""Create the initial payment service schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_STATUS_TYPE = sa.Enum(
    "CREATED",
    "PROCESSING",
    "COMPLETED",
    "REJECTED",
    name="operation_status",
    native_enum=False,
    create_constraint=False,
    length=16,
)
_INTENT_STATUS_TYPE = sa.Enum(
    "PENDING",
    "IN_FLIGHT",
    "RETRY_WAIT",
    "ACCEPTED",
    "RESOLVED",
    "BLOCKED",
    name="submission_intent_status",
    native_enum=False,
    create_constraint=False,
    length=16,
)
_RECEIPT_RESULT_TYPE = sa.Enum(
    "COMPLETED",
    "REJECTED",
    name="receipt_result",
    native_enum=False,
    create_constraint=False,
    length=16,
)
_RECEIPT_DISPOSITION_TYPE = sa.Enum(
    "APPLIED",
    "IGNORED_ALREADY_FINAL",
    "IGNORED_CONFLICTING_RESULT",
    "REJECTED_PROVIDER_ID_CONFLICT",
    name="receipt_disposition",
    native_enum=False,
    create_constraint=False,
    length=32,
)


def upgrade() -> None:
    """Create the initial schema."""
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            _OPERATION_STATUS_TYPE,
            server_default=sa.text("'CREATED'"),
            nullable=False,
        ),
        sa.Column("provider_payment_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(operation_id)) > 0",
            name="ck_operations_operation_id_not_blank",
        ),
        sa.CheckConstraint(
            "amount > 0 AND scale(amount) BETWEEN 0 AND 2",
            name="ck_operations_amount_positive_scale",
        ),
        sa.CheckConstraint("currency = 'RUB'", name="ck_operations_currency_rub"),
        sa.CheckConstraint(
            "status IN ('CREATED', 'PROCESSING', 'COMPLETED', 'REJECTED')",
            name="ck_operations_status",
        ),
        sa.CheckConstraint(
            "provider_payment_id IS NULL OR char_length(btrim(provider_payment_id)) > 0",
            name="ck_operations_provider_payment_id_not_blank",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_operations_timestamps",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint(
            "provider_payment_id",
            name="uq_operations_provider_payment_id",
        ),
    )
    op.create_index(
        "ix_operations_processing",
        "operations",
        ["updated_at", "operation_id"],
        unique=False,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )

    op.create_table(
        "submission_intents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "status",
            _INTENT_STATUS_TYPE,
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_http_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id > 0", name="ck_submission_intents_id_positive"),
        sa.CheckConstraint(
            "idempotency_key = operation_id",
            name="ck_submission_intents_idempotency_key",
        ),
        sa.CheckConstraint(
            "correlation_id = operation_id",
            name="ck_submission_intents_correlation_id",
        ),
        sa.CheckConstraint(
            "status IN "
            "('PENDING', 'IN_FLIGHT', 'RETRY_WAIT', 'ACCEPTED', 'RESOLVED', 'BLOCKED')",
            name="ck_submission_intents_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_submission_intents_attempt_count",
        ),
        sa.CheckConstraint(
            "last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599",
            name="ck_submission_intents_http_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object' "
            "AND request_payload - ARRAY['operationId', 'amount', 'currency'] = '{}'::jsonb "
            "AND request_payload ?& ARRAY['operationId', 'amount', 'currency'] "
            "AND request_payload ->> 'operationId' = operation_id "
            "AND request_payload ->> 'currency' = 'RUB'",
            name="ck_submission_intents_payload",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'RETRY_WAIT') "
            "AND next_attempt_at IS NOT NULL AND lease_until IS NULL) "
            "OR (status = 'IN_FLIGHT' "
            "AND next_attempt_at IS NULL AND lease_until IS NOT NULL) "
            "OR (status IN ('ACCEPTED', 'RESOLVED', 'BLOCKED') "
            "AND next_attempt_at IS NULL AND lease_until IS NULL)",
            name="ck_submission_intents_schedule_state",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_submission_intents_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_submission_intents_operation_id",
        ),
    )
    op.create_index(
        "ix_submission_intents_ready",
        "submission_intents",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('PENDING', 'RETRY_WAIT')"),
    )
    op.create_index(
        "ix_submission_intents_expired_lease",
        "submission_intents",
        ["lease_until", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'IN_FLIGHT'"),
    )

    op.create_table(
        "operation_events",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", _OPERATION_STATUS_TYPE, nullable=False),
        sa.Column("from_status", _OPERATION_STATUS_TYPE, nullable=True),
        sa.Column("to_status", _OPERATION_STATUS_TYPE, nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_id >= 1",
            name="ck_operation_events_event_id_positive",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="ck_operation_events_metadata_object",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", "event_id"),
        sa.UniqueConstraint(
            "operation_id",
            "type",
            name="uq_operation_events_operation_type",
        ),
    )
    op.create_index(
        "uq_operation_events_one_final",
        "operation_events",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("type IN ('COMPLETED', 'REJECTED')"),
    )

    op.create_table(
        "receipts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=False),
        sa.Column("result", _RECEIPT_RESULT_TYPE, nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "provider_occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "delivery_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("disposition", _RECEIPT_DISPOSITION_TYPE, nullable=False),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint("id > 0", name="ck_receipts_id_positive"),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_receipts_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "char_length(btrim(provider_payment_id)) > 0",
            name="ck_receipts_provider_payment_id_not_blank",
        ),
        sa.CheckConstraint(
            "result IN ('COMPLETED', 'REJECTED')",
            name="ck_receipts_result",
        ),
        sa.CheckConstraint(
            "disposition IN "
            "('APPLIED', 'IGNORED_ALREADY_FINAL', 'IGNORED_CONFLICTING_RESULT', "
            "'REJECTED_PROVIDER_ID_CONFLICT')",
            name="ck_receipts_disposition",
        ),
        sa.CheckConstraint(
            "delivery_count >= 1",
            name="ck_receipts_delivery_count",
        ),
        sa.CheckConstraint(
            "last_received_at >= received_at",
            name="ck_receipts_received_timestamps",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_receipts_fingerprint"),
    )
    op.create_index(
        "ix_receipts_operation_received",
        "receipts",
        ["operation_id", "received_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_receipts_provider_payment_id",
        "receipts",
        ["provider_payment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the initial schema in reverse dependency order."""
    op.drop_index("ix_receipts_provider_payment_id", table_name="receipts")
    op.drop_index("ix_receipts_operation_received", table_name="receipts")
    op.drop_table("receipts")

    op.drop_index(
        "uq_operation_events_one_final",
        table_name="operation_events",
    )
    op.drop_table("operation_events")

    op.drop_index(
        "ix_submission_intents_expired_lease",
        table_name="submission_intents",
    )
    op.drop_index(
        "ix_submission_intents_ready",
        table_name="submission_intents",
    )
    op.drop_table("submission_intents")

    op.drop_index("ix_operations_processing", table_name="operations")
    op.drop_table("operations")
