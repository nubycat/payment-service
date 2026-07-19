import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import OperationStatus, SubmissionIntentStatus
from app.models import Operation, SubmissionIntent
from app.provider.client import (
    NonRetryableProviderError,
    ProviderAcceptedResponse,
    ProviderClient,
    RetryableProviderError,
)

_MAX_RETRY_DELAY_SECONDS = 15 * 60
_DEFAULT_LEASE_DURATION = timedelta(seconds=30)

type Jitter = Callable[[], float]
type SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ClaimedIntent:
    intent_id: int
    operation_id: str
    request_payload: dict[str, Any]
    attempt_count: int


def base_retry_delay_seconds(attempt_count: int) -> float:
    if attempt_count < 1:
        raise ValueError("attempt_count must be at least 1")
    if attempt_count == 1:
        return 1.0
    if attempt_count == 2:
        return 2.0
    return float(min(30 * 2 ** (attempt_count - 3), _MAX_RETRY_DELAY_SECONDS))


def retry_delay(
    attempt_count: int,
    jitter: Jitter | None = None,
) -> timedelta:
    base_delay = base_retry_delay_seconds(attempt_count)
    random_value = (jitter or random.random)()
    if not 0.0 <= random_value <= 1.0:
        raise ValueError("jitter value must be between 0 and 1")

    if attempt_count <= 2:
        delay_seconds = base_delay * random_value
    else:
        delay_seconds = base_delay * (0.8 + 0.4 * random_value)

    return timedelta(seconds=min(delay_seconds, _MAX_RETRY_DELAY_SECONDS))


async def recover_submission_intents(
    session_factory: SessionFactory,
    *,
    now_factory: Callable[[], datetime] | None = None,
) -> int:
    now = (now_factory or _utc_now)()
    processing_operation_ids = select(Operation.operation_id).where(
        Operation.status == OperationStatus.PROCESSING
    )

    async with session_factory() as session, session.begin():
        statement = (
            update(SubmissionIntent)
            .where(
                SubmissionIntent.operation_id.in_(processing_operation_ids),
                SubmissionIntent.status.in_(
                    (
                        SubmissionIntentStatus.IN_FLIGHT,
                        SubmissionIntentStatus.ACCEPTED,
                    )
                ),
            )
            .values(
                status=SubmissionIntentStatus.RETRY_WAIT,
                next_attempt_at=now,
                lease_until=None,
                updated_at=now,
            )
        )
        result = await session.execute(statement)

    return result.rowcount


async def process_one_intent(
    session_factory: SessionFactory,
    provider_client: ProviderClient,
    *,
    now_factory: Callable[[], datetime] | None = None,
    lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    jitter: Jitter | None = None,
) -> bool:
    clock = now_factory or _utc_now
    claim = await _claim_one_intent(
        session_factory,
        now=clock(),
        lease_duration=lease_duration,
    )
    if claim is None:
        return False

    try:
        provider_response = await provider_client.submit_payment(
            claim.operation_id,
            claim.request_payload,
        )
    except RetryableProviderError as error:
        await _finalize_retryable_error(
            session_factory,
            claim,
            error,
            now=clock(),
            jitter=jitter,
        )
    except NonRetryableProviderError as error:
        await _finalize_non_retryable_error(
            session_factory,
            claim,
            error,
            now=clock(),
        )
    else:
        await _finalize_accepted(
            session_factory,
            claim,
            provider_response,
            now=clock(),
        )

    return True


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _claim_one_intent(
    session_factory: SessionFactory,
    *,
    now: datetime,
    lease_duration: timedelta,
) -> ClaimedIntent | None:
    async with session_factory() as session, session.begin():
        intent = await session.scalar(
            select(SubmissionIntent)
            .where(
                or_(
                    and_(
                        SubmissionIntent.status.in_(
                            (
                                SubmissionIntentStatus.PENDING,
                                SubmissionIntentStatus.RETRY_WAIT,
                            )
                        ),
                        SubmissionIntent.next_attempt_at <= now,
                    ),
                    and_(
                        SubmissionIntent.status == SubmissionIntentStatus.IN_FLIGHT,
                        SubmissionIntent.lease_until <= now,
                    ),
                )
            )
            .order_by(
                func.coalesce(
                    SubmissionIntent.next_attempt_at,
                    SubmissionIntent.lease_until,
                ),
                SubmissionIntent.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if intent is None:
            return None

        intent.status = SubmissionIntentStatus.IN_FLIGHT
        intent.next_attempt_at = None
        intent.lease_until = now + lease_duration
        intent.attempt_count += 1
        intent.updated_at = now
        await session.flush()

        return ClaimedIntent(
            intent_id=intent.id,
            operation_id=intent.operation_id,
            request_payload=intent.request_payload,
            attempt_count=intent.attempt_count,
        )


async def _lock_current_intent(
    session: AsyncSession,
    claim: ClaimedIntent,
) -> SubmissionIntent | None:
    intent = await session.scalar(
        select(SubmissionIntent).where(SubmissionIntent.id == claim.intent_id).with_for_update()
    )
    if (
        intent is None
        or intent.status != SubmissionIntentStatus.IN_FLIGHT
        or intent.attempt_count != claim.attempt_count
    ):
        return None
    return intent


async def _finalize_accepted(
    session_factory: SessionFactory,
    claim: ClaimedIntent,
    provider_response: ProviderAcceptedResponse,
    *,
    now: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        intent = await _lock_current_intent(session, claim)
        if intent is None:
            return

        operation = await session.scalar(
            select(Operation).where(Operation.operation_id == claim.operation_id).with_for_update()
        )
        if operation is None:
            _block_intent(intent, "Operation disappeared during dispatch", now=now)
            return

        provider_payment_id = provider_response.provider_payment_id
        if (
            operation.provider_payment_id is not None
            and operation.provider_payment_id != provider_payment_id
        ):
            _block_intent(
                intent,
                "Provider payment ID conflict: "
                f"stored={operation.provider_payment_id}, received={provider_payment_id}",
                now=now,
                http_status=202,
            )
            return

        if operation.provider_payment_id is None:
            operation.provider_payment_id = provider_payment_id
            operation.updated_at = now

        intent.status = SubmissionIntentStatus.ACCEPTED
        intent.next_attempt_at = None
        intent.lease_until = None
        intent.last_error = None
        intent.last_http_status = 202
        intent.updated_at = now


async def _finalize_retryable_error(
    session_factory: SessionFactory,
    claim: ClaimedIntent,
    error: RetryableProviderError,
    *,
    now: datetime,
    jitter: Jitter | None,
) -> None:
    async with session_factory() as session, session.begin():
        intent = await _lock_current_intent(session, claim)
        if intent is None:
            return

        intent.status = SubmissionIntentStatus.RETRY_WAIT
        intent.next_attempt_at = now + retry_delay(intent.attempt_count, jitter)
        intent.lease_until = None
        intent.last_error = str(error)
        intent.last_http_status = None
        intent.updated_at = now


async def _finalize_non_retryable_error(
    session_factory: SessionFactory,
    claim: ClaimedIntent,
    error: NonRetryableProviderError,
    *,
    now: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        intent = await _lock_current_intent(session, claim)
        if intent is None:
            return

        _block_intent(intent, str(error), now=now)


def _block_intent(
    intent: SubmissionIntent,
    message: str,
    *,
    now: datetime,
    http_status: int | None = None,
) -> None:
    intent.status = SubmissionIntentStatus.BLOCKED
    intent.next_attempt_at = None
    intent.lease_until = None
    intent.last_error = message
    intent.last_http_status = http_status
    intent.updated_at = now
