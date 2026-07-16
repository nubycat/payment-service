import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from app.database import async_session_factory, engine
from app.enums import OperationStatus, SubmissionIntentStatus
from app.models import Operation, SubmissionIntent
from app.provider.client import ProviderClient
from app.services.dispatch import (
    base_retry_delay_seconds,
    process_one_intent,
    retry_delay,
)
from sqlalchemy import delete, null

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def isolated_dispatch_database() -> AsyncIterator[None]:
    await engine.dispose(close=False)
    try:
        async with async_session_factory() as session, session.begin():
            await session.execute(
                delete(SubmissionIntent).where(
                    SubmissionIntent.operation_id.like("dispatch-%")
                )
            )
            await session.execute(
                delete(Operation).where(Operation.operation_id.like("dispatch-%"))
            )
        yield
    finally:
        await engine.dispose()


def accepted_response(
    request: httpx.Request,
    provider_payment_id: str,
) -> httpx.Response:
    return httpx.Response(
        202,
        json={
            "providerPaymentId": provider_payment_id,
            "status": "ACCEPTED",
        },
        request=request,
    )


def make_provider_client(
    transport: httpx.MockTransport,
    http_client: httpx.AsyncClient,
) -> ProviderClient:
    return ProviderClient(
        provider_url="http://provider.test",
        timeout_seconds=1.0,
        http_client=http_client,
    )


async def create_dispatch_state(
    *,
    operation_status: OperationStatus = OperationStatus.PROCESSING,
    provider_payment_id: str | None = None,
    intent_status: SubmissionIntentStatus = SubmissionIntentStatus.PENDING,
    next_attempt_at: datetime | None = NOW,
    lease_until: datetime | None = None,
    attempt_count: int = 0,
) -> tuple[str, int]:
    operation_id = f"dispatch-{uuid4()}"
    operation = Operation(
        operation_id=operation_id,
        amount=Decimal("125.40"),
        currency="RUB",
        status=operation_status,
        provider_payment_id=provider_payment_id,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    intent = SubmissionIntent(
        operation_id=operation_id,
        idempotency_key=operation_id,
        correlation_id=operation_id,
        request_payload={
            "operationId": operation_id,
            "amount": "125.40",
            "currency": "RUB",
        },
        status=intent_status,
        next_attempt_at=null() if next_attempt_at is None else next_attempt_at,
        lease_until=lease_until,
        attempt_count=attempt_count,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    async with async_session_factory() as session, session.begin():
        session.add(operation)
        await session.flush()
        session.add(intent)
        await session.flush()
        intent_id = intent.id
    return operation_id, intent_id


async def load_state(
    operation_id: str,
    intent_id: int,
) -> tuple[Operation, SubmissionIntent]:
    async with async_session_factory() as session:
        operation = await session.get(Operation, operation_id)
        intent = await session.get(SubmissionIntent, intent_id)
        assert operation is not None
        assert intent is not None
        return operation, intent


@pytest.mark.parametrize(
    ("attempt_count", "expected_seconds"),
    [
        (1, 1.0),
        (2, 2.0),
        (3, 30.0),
        (4, 60.0),
        (5, 120.0),
        (6, 240.0),
        (7, 480.0),
        (8, 900.0),
        (9, 900.0),
    ],
)
def test_retry_delay_policy(attempt_count: int, expected_seconds: float) -> None:
    assert base_retry_delay_seconds(attempt_count) == expected_seconds


@pytest.mark.parametrize(
    ("attempt_count", "random_value", "expected_seconds"),
    [
        (1, 0.0, 0.0),
        (1, 1.0, 1.0),
        (2, 0.0, 0.0),
        (2, 1.0, 2.0),
        (3, 0.0, 24.0),
        (3, 1.0, 36.0),
        (4, 0.0, 48.0),
        (4, 1.0, 72.0),
        (8, 0.0, 720.0),
        (8, 1.0, 900.0),
        (9, 1.0, 900.0),
    ],
)
def test_retry_delay_jitter_boundaries(
    attempt_count: int,
    random_value: float,
    expected_seconds: float,
) -> None:
    delay = retry_delay(attempt_count, jitter=lambda: random_value)

    assert delay.total_seconds() == expected_seconds


@pytest.mark.anyio
async def test_successful_202_accepts_intent_and_calls_provider_once(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    provider_payment_id = f"provider-{uuid4()}"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return accepted_response(request, provider_payment_id)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)

        first_processed = await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )
        second_processed = await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert first_processed is True
    assert second_processed is False
    assert calls == 1
    assert intent.status == SubmissionIntentStatus.ACCEPTED
    assert intent.lease_until is None
    assert intent.next_attempt_at is None
    assert operation.provider_payment_id == provider_payment_id


@pytest.mark.anyio
async def test_success_keeps_operation_processing(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    provider_payment_id = f"provider-{uuid4()}"
    transport = httpx.MockTransport(
        lambda request: accepted_response(request, provider_payment_id)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, _ = await load_state(operation_id, intent_id)
    assert operation.status == OperationStatus.PROCESSING


@pytest.mark.anyio
async def test_retryable_error_schedules_retry(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
            jitter=lambda: 0.0,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert operation.status == OperationStatus.PROCESSING
    assert intent.status == SubmissionIntentStatus.RETRY_WAIT
    assert intent.attempt_count == 1
    assert intent.last_error
    assert intent.lease_until is None


@pytest.mark.anyio
async def test_non_retryable_error_blocks_intent(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert operation.status == OperationStatus.PROCESSING
    assert intent.status == SubmissionIntentStatus.BLOCKED
    assert intent.last_error
    assert intent.lease_until is None
    assert intent.next_attempt_at is None


@pytest.mark.anyio
async def test_provider_payment_id_conflict_blocks_without_overwrite(
    isolated_dispatch_database: None,
) -> None:
    existing_provider_id = f"provider-existing-{uuid4()}"
    received_provider_id = f"provider-received-{uuid4()}"
    operation_id, intent_id = await create_dispatch_state(
        provider_payment_id=existing_provider_id
    )
    transport = httpx.MockTransport(
        lambda request: accepted_response(request, received_provider_id)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert operation.provider_payment_id == existing_provider_id
    assert operation.status == OperationStatus.PROCESSING
    assert intent.status == SubmissionIntentStatus.BLOCKED
    assert "conflict" in (intent.last_error or "").lower()


@pytest.mark.anyio
async def test_late_202_after_callback_preserves_final_status(
    isolated_dispatch_database: None,
) -> None:
    provider_payment_id = f"provider-final-{uuid4()}"
    operation_id, intent_id = await create_dispatch_state(
        operation_status=OperationStatus.COMPLETED,
        provider_payment_id=provider_payment_id,
    )
    transport = httpx.MockTransport(
        lambda request: accepted_response(request, provider_payment_id)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == provider_payment_id
    assert intent.status == SubmissionIntentStatus.ACCEPTED


@pytest.mark.anyio
async def test_next_attempt_at_uses_deterministic_jitter(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
            jitter=lambda: 1.0,
        )

    _, intent = await load_state(operation_id, intent_id)
    assert intent.attempt_count == 1
    assert intent.next_attempt_at == NOW + timedelta(seconds=1)


@pytest.mark.anyio
async def test_expired_lease_is_reclaimed(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state(
        intent_status=SubmissionIntentStatus.IN_FLIGHT,
        next_attempt_at=None,
        lease_until=NOW - timedelta(seconds=1),
        attempt_count=1,
    )
    provider_payment_id = f"provider-reclaimed-{uuid4()}"
    transport = httpx.MockTransport(
        lambda request: accepted_response(request, provider_payment_id)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        processed = await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )

    operation, intent = await load_state(operation_id, intent_id)
    assert processed is True
    assert intent.attempt_count == 2
    assert intent.status == SubmissionIntentStatus.ACCEPTED
    assert operation.provider_payment_id == provider_payment_id


@pytest.mark.anyio
async def test_two_processors_do_not_claim_same_intent(
    isolated_dispatch_database: None,
) -> None:
    operation_id, intent_id = await create_dispatch_state()
    provider_payment_id = f"provider-concurrent-{uuid4()}"
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_started.set()
        await release_request.wait()
        return accepted_response(request, provider_payment_id)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = make_provider_client(transport, http_client)
        first_processor = asyncio.create_task(
            process_one_intent(
                async_session_factory,
                client,
                now_factory=lambda: NOW,
            )
        )
        await request_started.wait()

        second_processed = await process_one_intent(
            async_session_factory,
            client,
            now_factory=lambda: NOW,
        )
        release_request.set()
        first_processed = await first_processor

    operation, intent = await load_state(operation_id, intent_id)
    assert first_processed is True
    assert second_processed is False
    assert calls == 1
    assert intent.attempt_count == 1
    assert intent.status == SubmissionIntentStatus.ACCEPTED
    assert operation.provider_payment_id == provider_payment_id
