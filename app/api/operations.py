from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import OperationCreate, OperationEventResponse, OperationResponse
from app.services.operations import (
    OperationAlreadyExistsError,
    OperationNotFoundError,
    create_operation,
    get_operation,
    get_operation_events,
)

router = APIRouter(prefix="/operations", tags=["operations"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=OperationResponse, status_code=status.HTTP_201_CREATED)
async def post_operation(
    data: OperationCreate,
    session: SessionDependency,
) -> OperationResponse:
    try:
        operation = await create_operation(session, data)
    except OperationAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operation already exists",
        ) from error
    return OperationResponse.model_validate(operation)


@router.get("/{operation_id}", response_model=OperationResponse)
async def read_operation(
    operation_id: str,
    session: SessionDependency,
) -> OperationResponse:
    try:
        operation = await get_operation(session, operation_id)
    except OperationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Operation not found",
        ) from error
    return OperationResponse.model_validate(operation)


@router.get("/{operation_id}/events", response_model=list[OperationEventResponse])
async def read_operation_events(
    operation_id: str,
    session: SessionDependency,
) -> list[OperationEventResponse]:
    try:
        events = await get_operation_events(session, operation_id)
    except OperationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Operation not found",
        ) from error
    return [OperationEventResponse.model_validate(event) for event in events]
