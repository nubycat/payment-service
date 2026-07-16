from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OperationStatus
from app.models import Operation, OperationEvent, SubmissionIntent
from app.schemas import OperationCreate


@dataclass(frozen=True)
class SubmitOperationResult:
    operation: Operation
    submitted: bool

class OperationAlreadyExistsError(Exception):
    pass


class OperationNotFoundError(Exception):
    pass


async def create_operation(session: AsyncSession, data: OperationCreate) -> Operation:
    operation = Operation(
        operation_id=data.operation_id,
        amount=data.amount,
        currency=data.currency,
        description=data.description,
        status=OperationStatus.CREATED,
    )
    event = OperationEvent(
        operation_id=data.operation_id,
        event_id=1,
        event_type=OperationStatus.CREATED,
        from_status=None,
        to_status=OperationStatus.CREATED,
        message="Operation created",
        event_metadata={},
    )

    try:
        async with session.begin():
            session.add_all((operation, event))
            await session.flush()
            await session.refresh(operation)
    except IntegrityError as error:
        raise OperationAlreadyExistsError(data.operation_id) from error

    return operation


async def get_operation(session: AsyncSession, operation_id: str) -> Operation:
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFoundError(operation_id)
    return operation


async def get_operation_events(
    session: AsyncSession,
    operation_id: str,
) -> list[OperationEvent]:
    if await session.get(Operation, operation_id) is None:
        raise OperationNotFoundError(operation_id)

    result = await session.scalars(
        select(OperationEvent)
        .where(OperationEvent.operation_id == operation_id)
        .order_by(OperationEvent.event_id.asc())
    )
    return list(result)

async def submit_operation(
    session: AsyncSession,
    operation_id: str,
) -> SubmitOperationResult:
    async with session.begin():
        operation = await session.scalar(
            select(Operation)
            .where(Operation.operation_id == operation_id)
            .with_for_update()
        )
        if operation is None:
            raise OperationNotFoundError(operation_id)

        if operation.status != OperationStatus.CREATED:
            return SubmitOperationResult(operation=operation, submitted=False)

        next_event_id = await session.scalar(
            select(func.coalesce(func.max(OperationEvent.event_id), 0) + 1).where(
                OperationEvent.operation_id == operation_id
            )
        )
        intent = SubmissionIntent(
            operation_id=operation.operation_id,
            idempotency_key=operation.operation_id,
            correlation_id=operation.operation_id,
            request_payload={
                "operationId": operation.operation_id,
                "amount": format(operation.amount, "f"),
                "currency": operation.currency,
            },
        )
        event = OperationEvent(
            operation_id=operation.operation_id,
            event_id=int(next_event_id or 1),
            event_type=OperationStatus.PROCESSING,
            from_status=OperationStatus.CREATED,
            to_status=OperationStatus.PROCESSING,
            message="Operation submitted",
            event_metadata={},
        )
        operation.status = OperationStatus.PROCESSING
        session.add_all((intent, event))
        await session.flush()
        await session.refresh(operation)

    return SubmitOperationResult(operation=operation, submitted=True)
