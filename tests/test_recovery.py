from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from app.config import get_settings
from app.database import Base
from app.enums import OperationStatus, SubmissionIntentStatus
from app.models import Operation, SubmissionIntent
from app.services.dispatch import recover_submission_intents
from sqlalchemy import null
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
CREATED_AT = NOW - timedelta(days=1)

type RecoverySessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def recovery_session_factory() -> AsyncIterator[RecoverySessionFactory]:
    schema_name = f"recovery_test_{uuid4().hex}"
    engine = create_async_engine(get_settings().database_url)
    isolated_engine = engine.execution_options(schema_translate_map={None: schema_name})

    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        async with isolated_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        yield async_sessionmaker(
            bind=isolated_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    finally:
        async with isolated_engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema_name, if_exists=True, cascade=True))
        await engine.dispose()


async def create_recovery_state(
    session_factory: RecoverySessionFactory,
    *,
    operation_status: OperationStatus = OperationStatus.PROCESSING,
    intent_status: SubmissionIntentStatus,
    next_attempt_at: datetime | None,
    lease_until: datetime | None,
    attempt_count: int = 4,
    provider_payment_id: str | None = None,
) -> tuple[str, int, dict[str, str]]:
    operation_id = f"recovery-{uuid4()}"
    payload = {
        "operationId": operation_id,
        "amount": "1000.00",
        "currency": "RUB",
    }
    operation = Operation(
        operation_id=operation_id,
        amount=Decimal("1000.00"),
        currency="RUB",
        status=operation_status,
        provider_payment_id=provider_payment_id,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    intent = SubmissionIntent(
        operation_id=operation_id,
        idempotency_key=operation_id,
        correlation_id=operation_id,
        request_payload=payload,
        status=intent_status,
        next_attempt_at=null() if next_attempt_at is None else next_attempt_at,
        lease_until=lease_until,
        attempt_count=attempt_count,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    async with session_factory() as session, session.begin():
        session.add(operation)
        await session.flush()
        session.add(intent)
        await session.flush()
        intent_id = intent.id

    return operation_id, intent_id, payload


async def load_state(
    session_factory: RecoverySessionFactory,
    operation_id: str,
    intent_id: int,
) -> tuple[Operation, SubmissionIntent]:
    async with session_factory() as session:
        operation = await session.get(Operation, operation_id)
        intent = await session.get(SubmissionIntent, intent_id)
        assert operation is not None
        assert intent is not None
        return operation, intent


@pytest.mark.anyio
async def test_in_flight_becomes_immediately_ready_without_changing_request(
    recovery_session_factory: RecoverySessionFactory,
) -> None:
    operation_id, intent_id, payload = await create_recovery_state(
        recovery_session_factory,
        intent_status=SubmissionIntentStatus.IN_FLIGHT,
        next_attempt_at=None,
        lease_until=NOW + timedelta(minutes=10),
    )

    recovered = await recover_submission_intents(
        recovery_session_factory,
        now_factory=lambda: NOW,
    )

    _, intent = await load_state(recovery_session_factory, operation_id, intent_id)
    assert recovered == 1
    assert intent.status == SubmissionIntentStatus.RETRY_WAIT
    assert intent.next_attempt_at == NOW
    assert intent.lease_until is None
    assert intent.attempt_count == 4
    assert intent.request_payload == payload
    assert intent.idempotency_key == operation_id
    assert intent.correlation_id == operation_id


@pytest.mark.anyio
async def test_accepted_processing_becomes_ready_and_keeps_provider_id(
    recovery_session_factory: RecoverySessionFactory,
) -> None:
    provider_payment_id = f"provider-{uuid4()}"
    operation_id, intent_id, payload = await create_recovery_state(
        recovery_session_factory,
        intent_status=SubmissionIntentStatus.ACCEPTED,
        next_attempt_at=None,
        lease_until=None,
        provider_payment_id=provider_payment_id,
    )

    recovered = await recover_submission_intents(
        recovery_session_factory,
        now_factory=lambda: NOW,
    )

    operation, intent = await load_state(recovery_session_factory, operation_id, intent_id)
    assert recovered == 1
    assert operation.provider_payment_id == provider_payment_id
    assert intent.status == SubmissionIntentStatus.RETRY_WAIT
    assert intent.next_attempt_at == NOW
    assert intent.lease_until is None
    assert intent.attempt_count == 4
    assert intent.request_payload == payload
    assert intent.idempotency_key == operation_id
    assert intent.correlation_id == operation_id


@pytest.mark.parametrize(
    ("intent_status", "next_attempt_at"),
    [
        (SubmissionIntentStatus.PENDING, NOW + timedelta(days=3650)),
        (SubmissionIntentStatus.RETRY_WAIT, NOW + timedelta(days=3650)),
        (SubmissionIntentStatus.RESOLVED, None),
        (SubmissionIntentStatus.BLOCKED, None),
    ],
)
@pytest.mark.anyio
async def test_other_intent_statuses_are_not_changed(
    recovery_session_factory: RecoverySessionFactory,
    intent_status: SubmissionIntentStatus,
    next_attempt_at: datetime | None,
) -> None:
    operation_id, intent_id, _ = await create_recovery_state(
        recovery_session_factory,
        intent_status=intent_status,
        next_attempt_at=next_attempt_at,
        lease_until=None,
    )

    recovered = await recover_submission_intents(
        recovery_session_factory,
        now_factory=lambda: NOW,
    )

    _, intent = await load_state(recovery_session_factory, operation_id, intent_id)
    assert recovered == 0
    assert intent.status == intent_status
    assert intent.next_attempt_at == next_attempt_at
    assert intent.updated_at == CREATED_AT


@pytest.mark.parametrize(
    "operation_status",
    [OperationStatus.COMPLETED, OperationStatus.REJECTED],
)
@pytest.mark.anyio
async def test_accepted_final_operation_is_not_recovered(
    recovery_session_factory: RecoverySessionFactory,
    operation_status: OperationStatus,
) -> None:
    operation_id, intent_id, _ = await create_recovery_state(
        recovery_session_factory,
        operation_status=operation_status,
        intent_status=SubmissionIntentStatus.ACCEPTED,
        next_attempt_at=None,
        lease_until=None,
        provider_payment_id=f"provider-{uuid4()}",
    )

    recovered = await recover_submission_intents(
        recovery_session_factory,
        now_factory=lambda: NOW,
    )

    _, intent = await load_state(recovery_session_factory, operation_id, intent_id)
    assert recovered == 0
    assert intent.status == SubmissionIntentStatus.ACCEPTED
    assert intent.next_attempt_at is None
    assert intent.updated_at == CREATED_AT
