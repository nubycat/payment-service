from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OperationStatus
from app.models import Operation, OperationEvent
from app.schemas import OperationCreate


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
