from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ReceiptCreate
from app.services.receipts import (
    ReceiptOperationNotFoundError,
    ReceiptOperationNotProcessingError,
    ReceiptProviderPaymentIdConflictError,
    process_receipt,
)

router = APIRouter(prefix="/receipts", tags=["receipts"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def post_receipt(data: ReceiptCreate, session: SessionDependency) -> Response:
    try:
        await process_receipt(session, data)
    except ReceiptOperationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Operation not found",
        ) from error
    except ReceiptOperationNotProcessingError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operation has not been submitted",
        ) from error
    except ReceiptProviderPaymentIdConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Provider payment ID conflict",
        ) from error

    return Response(status_code=status.HTTP_204_NO_CONTENT)
