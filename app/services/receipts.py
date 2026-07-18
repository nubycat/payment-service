import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    OperationStatus,
    ReceiptDisposition,
    SubmissionIntentStatus,
)
from app.models import Operation, OperationEvent, Receipt, SubmissionIntent
from app.schemas import ReceiptCreate


class ReceiptOperationNotFoundError(Exception):
    pass


class ReceiptOperationNotProcessingError(Exception):
    pass


class ReceiptProviderPaymentIdConflictError(Exception):
    pass


async def process_receipt(session: AsyncSession, data: ReceiptCreate) -> None:
    received_at = datetime.now(UTC)
    raw_payload = _canonical_payload(data)
    fingerprint = _fingerprint(raw_payload)
    provider_id_conflict = False

    async with session.begin():
        intent = await session.scalar(
            select(SubmissionIntent)
            .where(SubmissionIntent.operation_id == data.operation_id)
            .with_for_update()
        )
        operation = await session.scalar(
            select(Operation)
            .where(Operation.operation_id == data.operation_id)
            .with_for_update()
        )
        if operation is None:
            raise ReceiptOperationNotFoundError(data.operation_id)
        if operation.status == OperationStatus.CREATED:
            raise ReceiptOperationNotProcessingError(data.operation_id)

        existing_receipt = await session.scalar(
            select(Receipt).where(Receipt.fingerprint == fingerprint)
        )
        if existing_receipt is not None:
            existing_receipt.delivery_count += 1
            existing_receipt.last_received_at = received_at
            if operation.status in (
                OperationStatus.COMPLETED,
                OperationStatus.REJECTED,
            ):
                _resolve_intent(intent, received_at)
            return

        if (
            operation.provider_payment_id is not None
            and operation.provider_payment_id != data.provider_payment_id
        ):
            disposition = ReceiptDisposition.REJECTED_PROVIDER_ID_CONFLICT
            provider_id_conflict = True
        elif operation.status in (
            OperationStatus.COMPLETED,
            OperationStatus.REJECTED,
        ):
            disposition = (
                ReceiptDisposition.IGNORED_ALREADY_FINAL
                if operation.status.value == data.result.value
                else ReceiptDisposition.IGNORED_CONFLICTING_RESULT
            )
            _resolve_intent(intent, received_at)
        else:
            disposition = ReceiptDisposition.APPLIED
            await _apply_final_transition(
                session,
                operation,
                intent,
                data,
                received_at,
            )

        session.add(
            Receipt(
                fingerprint=fingerprint,
                operation_id=data.operation_id,
                provider_payment_id=data.provider_payment_id,
                result=data.result,
                message=data.message,
                provider_occurred_at=data.occurred_at.astimezone(UTC),
                received_at=received_at,
                last_received_at=received_at,
                disposition=disposition,
                raw_payload=raw_payload,
            )
        )
        await session.flush()

    if provider_id_conflict:
        raise ReceiptProviderPaymentIdConflictError(data.operation_id)


async def _apply_final_transition(
    session: AsyncSession,
    operation: Operation,
    intent: SubmissionIntent | None,
    data: ReceiptCreate,
    received_at: datetime,
) -> None:
    next_event_id = await session.scalar(
        select(func.coalesce(func.max(OperationEvent.event_id), 0) + 1).where(
            OperationEvent.operation_id == operation.operation_id
        )
    )
    final_status = OperationStatus(data.result.value)

    if operation.provider_payment_id is None:
        operation.provider_payment_id = data.provider_payment_id
    operation.status = final_status
    operation.updated_at = received_at
    _resolve_intent(intent, received_at)

    session.add(
        OperationEvent(
            operation_id=operation.operation_id,
            event_id=int(next_event_id or 1),
            event_type=final_status,
            from_status=OperationStatus.PROCESSING,
            to_status=final_status,
            message=data.message,
            occurred_at=received_at,
            event_metadata={},
        )
    )


def _resolve_intent(intent: SubmissionIntent | None, resolved_at: datetime) -> None:
    if intent is None or intent.status == SubmissionIntentStatus.RESOLVED:
        return
    intent.status = SubmissionIntentStatus.RESOLVED
    intent.next_attempt_at = None
    intent.lease_until = None
    intent.last_error = None
    intent.updated_at = resolved_at


def _canonical_payload(data: ReceiptCreate) -> dict[str, str]:
    occurred_at = data.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "operationId": data.operation_id,
        "providerPaymentId": data.provider_payment_id,
        "result": data.result.value,
        "message": data.message,
        "occurredAt": occurred_at,
    }


def _fingerprint(payload: dict[str, str]) -> str:
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
