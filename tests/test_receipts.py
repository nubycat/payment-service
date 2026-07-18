from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.database import async_session_factory
from app.enums import OperationStatus, ReceiptDisposition
from app.models import Operation, OperationEvent, Receipt
from fastapi.testclient import TestClient
from sqlalchemy import select

OCCURRED_AT = "2026-07-18T12:00:00Z"
FINAL_STATUSES = (OperationStatus.COMPLETED, OperationStatus.REJECTED)


def operation_id(prefix: str) -> str:
    return f"receipt-{prefix}-{uuid4()}"


def provider_payment_id(prefix: str) -> str:
    return f"provider-{prefix}-{uuid4()}"


def operation_payload(identifier: str) -> dict[str, str]:
    return {
        "operationId": identifier,
        "amount": "100.25",
        "currency": "RUB",
    }


def receipt_payload(
    identifier: str,
    provider_identifier: str,
    *,
    result: str = "COMPLETED",
    message: str = "Payment completed",
    occurred_at: str = OCCURRED_AT,
) -> dict[str, str]:
    return {
        "providerPaymentId": provider_identifier,
        "operationId": identifier,
        "result": result,
        "message": message,
        "occurredAt": occurred_at,
    }


def create_processing_operation(
    client: TestClient,
    prefix: str,
    *,
    provider_identifier: str | None = None,
) -> str:
    identifier = operation_id(prefix)
    create_response = client.post("/operations", json=operation_payload(identifier))
    submit_response = client.post(f"/operations/{identifier}/submit")

    assert create_response.status_code == 201
    assert submit_response.status_code == 202
    if provider_identifier is not None:
        set_provider_payment_id(client, identifier, provider_identifier)
    return identifier


def set_provider_payment_id(
    client: TestClient,
    identifier: str,
    provider_identifier: str,
) -> None:
    async def update() -> None:
        async with async_session_factory() as session, session.begin():
            operation = await session.scalar(
                select(Operation)
                .where(Operation.operation_id == identifier)
                .with_for_update()
            )
            assert operation is not None
            operation.provider_payment_id = provider_identifier

    assert client.portal is not None
    client.portal.call(update)


def load_state(
    client: TestClient,
    identifier: str,
) -> tuple[Operation | None, list[Receipt], list[OperationEvent]]:
    async def load() -> tuple[Operation | None, list[Receipt], list[OperationEvent]]:
        async with async_session_factory() as session:
            operation = await session.get(Operation, identifier)
            receipts = list(
                await session.scalars(
                    select(Receipt)
                    .where(Receipt.operation_id == identifier)
                    .order_by(Receipt.id)
                )
            )
            events = list(
                await session.scalars(
                    select(OperationEvent)
                    .where(OperationEvent.operation_id == identifier)
                    .order_by(OperationEvent.event_id)
                )
            )
            return operation, receipts, events

    assert client.portal is not None
    return client.portal.call(load)


def final_events(events: list[OperationEvent]) -> list[OperationEvent]:
    return [event for event in events if event.event_type in FINAL_STATUSES]


def test_unknown_operation_returns_not_found_without_audit(
    client: TestClient,
) -> None:
    identifier = operation_id("unknown")
    payload = receipt_payload(identifier, provider_payment_id("unknown"))

    response = client.post("/receipts", json=payload)
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 404
    assert response.json() == {"detail": "Operation not found"}
    assert operation is None
    assert receipts == []
    assert events == []


def test_created_operation_returns_conflict_without_changes(
    client: TestClient,
) -> None:
    identifier = operation_id("created")
    client.post("/operations", json=operation_payload(identifier))

    response = client.post(
        "/receipts",
        json=receipt_payload(identifier, provider_payment_id("created")),
    )
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 409
    assert operation is not None
    assert operation.status == OperationStatus.CREATED
    assert operation.provider_payment_id is None
    assert receipts == []
    assert len(events) == 1
    assert final_events(events) == []


def test_early_callback_sets_provider_id_and_final_status(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("early")
    identifier = create_processing_operation(client, "early")
    before_callback = client.get(f"/operations/{identifier}")

    response = client.post(
        "/receipts",
        json=receipt_payload(identifier, provider_identifier),
    )
    operation, receipts, events = load_state(client, identifier)

    assert before_callback.json()["providerPaymentId"] is None
    assert response.status_code == 204
    assert response.content == b""
    assert operation is not None
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == provider_identifier
    assert len(receipts) == 1
    assert receipts[0].disposition == ReceiptDisposition.APPLIED
    assert len(final_events(events)) == 1
    assert final_events(events)[0].event_id == 3


def test_callback_with_matching_provider_id_is_applied(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("matching")
    identifier = create_processing_operation(
        client,
        "matching",
        provider_identifier=provider_identifier,
    )

    response = client.post(
        "/receipts",
        json=receipt_payload(
            identifier,
            provider_identifier,
            result="REJECTED",
            message="Payment rejected",
        ),
    )
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 204
    assert operation is not None
    assert operation.status == OperationStatus.REJECTED
    assert operation.provider_payment_id == provider_identifier
    assert [receipt.disposition for receipt in receipts] == [
        ReceiptDisposition.APPLIED
    ]
    assert len(final_events(events)) == 1
    assert final_events(events)[0].event_type == OperationStatus.REJECTED


def test_provider_id_conflict_commits_audit_before_returning_conflict(
    client: TestClient,
) -> None:
    stored_provider_id = provider_payment_id("stored")
    received_provider_id = provider_payment_id("received")
    identifier = create_processing_operation(
        client,
        "provider-conflict",
        provider_identifier=stored_provider_id,
    )

    response = client.post(
        "/receipts",
        json=receipt_payload(identifier, received_provider_id),
    )
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 409
    assert response.json() == {"detail": "Provider payment ID conflict"}
    assert operation is not None
    assert operation.status == OperationStatus.PROCESSING
    assert operation.provider_payment_id == stored_provider_id
    assert len(receipts) == 1
    assert (
        receipts[0].disposition
        == ReceiptDisposition.REJECTED_PROVIDER_ID_CONFLICT
    )
    assert final_events(events) == []


def test_exact_repeat_uses_normalized_canonical_fingerprint(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("repeat")
    identifier = create_processing_operation(client, "repeat")
    first_payload = receipt_payload(identifier, provider_identifier)
    reordered_payload = {
        "occurredAt": "2026-07-18T15:00:00+03:00",
        "message": "Payment completed",
        "result": "COMPLETED",
        "operationId": identifier,
        "providerPaymentId": provider_identifier,
    }

    first_response = client.post("/receipts", json=first_payload)
    repeated_response = client.post("/receipts", json=reordered_payload)
    operation, receipts, events = load_state(client, identifier)

    assert first_response.status_code == 204
    assert repeated_response.status_code == 204
    assert operation is not None
    assert operation.status == OperationStatus.COMPLETED
    assert len(receipts) == 1
    assert receipts[0].delivery_count == 2
    assert receipts[0].raw_payload["occurredAt"] == OCCURRED_AT
    assert len(receipts[0].fingerprint) == 64
    assert len(final_events(events)) == 1


def test_same_final_result_with_different_content_is_audited(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("same-final")
    identifier = create_processing_operation(client, "same-final")
    client.post(
        "/receipts",
        json=receipt_payload(identifier, provider_identifier),
    )

    response = client.post(
        "/receipts",
        json=receipt_payload(
            identifier,
            provider_identifier,
            message="Second delivery detail",
            occurred_at="2026-07-18T12:01:00Z",
        ),
    )
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 204
    assert operation is not None
    assert operation.status == OperationStatus.COMPLETED
    assert [receipt.disposition for receipt in receipts] == [
        ReceiptDisposition.APPLIED,
        ReceiptDisposition.IGNORED_ALREADY_FINAL,
    ]
    assert len(final_events(events)) == 1


def test_opposite_late_result_is_audited_without_state_change(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("opposite")
    identifier = create_processing_operation(client, "opposite")
    client.post(
        "/receipts",
        json=receipt_payload(identifier, provider_identifier),
    )

    response = client.post(
        "/receipts",
        json=receipt_payload(
            identifier,
            provider_identifier,
            result="REJECTED",
            message="Late rejection",
            occurred_at="2026-07-18T12:02:00Z",
        ),
    )
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 204
    assert operation is not None
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == provider_identifier
    assert [receipt.disposition for receipt in receipts] == [
        ReceiptDisposition.APPLIED,
        ReceiptDisposition.IGNORED_CONFLICTING_RESULT,
    ]
    assert len(final_events(events)) == 1


def test_invalid_result_returns_validation_error_without_writes(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("invalid")
    identifier = create_processing_operation(client, "invalid")
    payload = receipt_payload(identifier, provider_identifier)
    payload["result"] = "PENDING"

    response = client.post("/receipts", json=payload)
    operation, receipts, events = load_state(client, identifier)

    assert response.status_code == 422
    assert operation is not None
    assert operation.status == OperationStatus.PROCESSING
    assert operation.provider_payment_id is None
    assert receipts == []
    assert final_events(events) == []


def test_success_response_has_completely_empty_body(client: TestClient) -> None:
    provider_identifier = provider_payment_id("empty-body")
    identifier = create_processing_operation(client, "empty-body")

    response = client.post(
        "/receipts",
        json=receipt_payload(
            identifier,
            provider_identifier,
            result="REJECTED",
        ),
    )

    assert response.status_code == 204
    assert response.content == b""


def test_callbacks_create_only_one_final_event(client: TestClient) -> None:
    provider_identifier = provider_payment_id("one-final")
    identifier = create_processing_operation(client, "one-final")

    client.post(
        "/receipts",
        json=receipt_payload(identifier, provider_identifier),
    )
    client.post(
        "/receipts",
        json=receipt_payload(
            identifier,
            provider_identifier,
            result="REJECTED",
            message="Conflicting late result",
            occurred_at="2026-07-18T12:03:00Z",
        ),
    )
    _, _, events = load_state(client, identifier)

    assert len(final_events(events)) == 1
    assert [event.event_id for event in events] == [1, 2, 3]


def test_concurrent_identical_callbacks_are_idempotent(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("concurrent-same")
    identifier = create_processing_operation(client, "concurrent-same")
    payload = receipt_payload(identifier, provider_identifier)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: client.post("/receipts", json=payload),
                range(2),
            )
        )

    operation, receipts, events = load_state(client, identifier)
    assert [response.status_code for response in responses] == [204, 204]
    assert operation is not None
    assert operation.status == OperationStatus.COMPLETED
    assert operation.provider_payment_id == provider_identifier
    assert len(receipts) == 1
    assert receipts[0].delivery_count == 2
    assert len(final_events(events)) == 1


def test_concurrent_opposite_callbacks_preserve_first_final_result(
    client: TestClient,
) -> None:
    provider_identifier = provider_payment_id("concurrent-opposite")
    identifier = create_processing_operation(client, "concurrent-opposite")
    payloads = [
        receipt_payload(identifier, provider_identifier),
        receipt_payload(
            identifier,
            provider_identifier,
            result="REJECTED",
            message="Concurrent rejection",
        ),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda payload: client.post("/receipts", json=payload),
                payloads,
            )
        )

    operation, receipts, events = load_state(client, identifier)
    final = final_events(events)
    assert [response.status_code for response in responses] == [204, 204]
    assert operation is not None
    assert operation.status in FINAL_STATUSES
    assert operation.provider_payment_id == provider_identifier
    assert {receipt.disposition for receipt in receipts} == {
        ReceiptDisposition.APPLIED,
        ReceiptDisposition.IGNORED_CONFLICTING_RESULT,
    }
    assert len(final) == 1
    assert final[0].event_type == operation.status
