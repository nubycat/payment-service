from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import async_session_factory
from app.models import SubmissionIntent


def operation_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def create_payload(identifier: str, amount: str = "100.25") -> dict[str, str]:
    return {
        "operationId": identifier,
        "amount": amount,
        "currency": "RUB",
    }


def load_submission_intents(
    client: TestClient,
    identifier: str,
) -> list[SubmissionIntent]:
    async def load() -> list[SubmissionIntent]:
        async with async_session_factory() as session:
            result = await session.scalars(
                select(SubmissionIntent).where(
                    SubmissionIntent.operation_id == identifier
                )
            )
            return list(result)

    assert client.portal is not None
    return client.portal.call(load)

def test_create_operation(client: TestClient) -> None:
    identifier = operation_id("create")

    response = client.post("/operations", json=create_payload(identifier))

    assert response.status_code == 201
    body = response.json()
    assert body["operationId"] == identifier
    assert body["amount"] == "100.25"
    assert body["currency"] == "RUB"
    assert body["description"] is None
    assert body["status"] == "CREATED"
    assert body["providerPaymentId"] is None
    assert body["createdAt"]
    assert body["updatedAt"]


def test_amount_is_normalized(client: TestClient) -> None:
    identifier = operation_id("normalize")

    response = client.post("/operations", json=create_payload(identifier, amount="0010.5"))

    assert response.status_code == 201
    assert response.json()["amount"] == "10.50"


@pytest.mark.parametrize(
    "amount",
    ["0", "0.00", "-1", "1.001", "1.", ".5", "1e2", 10],
)
def test_invalid_amount(client: TestClient, amount: object) -> None:
    payload = create_payload(operation_id("invalid-amount"))
    payload["amount"] = amount  # type: ignore[assignment]

    response = client.post("/operations", json=payload)

    assert response.status_code == 422


def test_currency_must_be_rub(client: TestClient) -> None:
    payload = create_payload(operation_id("currency"))
    payload["currency"] = "USD"

    response = client.post("/operations", json=payload)

    assert response.status_code == 422


def test_duplicate_operation_id_returns_conflict(client: TestClient) -> None:
    identifier = operation_id("duplicate")
    payload = create_payload(identifier)

    first_response = client.post("/operations", json=payload)
    duplicate_response = client.post("/operations", json=payload)

    assert first_response.status_code == 201
    assert duplicate_response.status_code == 409
    assert duplicate_response.json() == {"detail": "Operation already exists"}


def test_get_operation(client: TestClient) -> None:
    identifier = operation_id("get")
    create_response = client.post("/operations", json=create_payload(identifier, amount="42"))

    response = client.get(f"/operations/{identifier}")

    assert create_response.status_code == 201
    assert response.status_code == 200
    assert response.json() == create_response.json()


def test_get_unknown_operation_returns_not_found(client: TestClient) -> None:
    response = client.get(f"/operations/{operation_id('unknown')}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Operation not found"}


def test_get_first_event(client: TestClient) -> None:
    identifier = operation_id("events")
    create_response = client.post("/operations", json=create_payload(identifier))

    response = client.get(f"/operations/{identifier}/events")

    assert create_response.status_code == 201
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["eventId"] == 1
    assert events[0]["type"] == "CREATED"
    assert events[0]["fromStatus"] is None
    assert events[0]["toStatus"] == "CREATED"
    assert events[0]["message"] == "Operation created"
    assert events[0]["occurredAt"]
    assert set(events[0]) == {
        "eventId",
        "type",
        "fromStatus",
        "toStatus",
        "message",
        "occurredAt",
    }

def test_first_submit_returns_accepted(client: TestClient) -> None:
    identifier = operation_id("submit-first")
    create_response = client.post("/operations", json=create_payload(identifier))

    response = client.post(f"/operations/{identifier}/submit")

    assert create_response.status_code == 201
    assert response.status_code == 202
    assert response.json()["operationId"] == identifier
    assert response.json()["status"] == "PROCESSING"


def test_repeated_submit_returns_current_state(client: TestClient) -> None:
    identifier = operation_id("submit-repeat")
    client.post("/operations", json=create_payload(identifier))
    first_response = client.post(f"/operations/{identifier}/submit")

    response = client.post(f"/operations/{identifier}/submit")

    assert first_response.status_code == 202
    assert response.status_code == 200
    assert response.json()["status"] == "PROCESSING"


def test_submit_unknown_operation_returns_not_found(client: TestClient) -> None:
    response = client.post(f"/operations/{operation_id('submit-unknown')}/submit")

    assert response.status_code == 404
    assert response.json() == {"detail": "Operation not found"}


def test_submit_creates_one_intent(client: TestClient) -> None:
    identifier = operation_id("submit-intent")
    client.post("/operations", json=create_payload(identifier))
    client.post(f"/operations/{identifier}/submit")
    client.post(f"/operations/{identifier}/submit")

    intents = load_submission_intents(client, identifier)

    assert len(intents) == 1
    assert intents[0].idempotency_key == identifier
    assert intents[0].correlation_id == identifier


def test_submit_preserves_request_payload(client: TestClient) -> None:
    identifier = operation_id("submit-payload")
    client.post("/operations", json=create_payload(identifier, amount="125.40"))

    response = client.post(f"/operations/{identifier}/submit")
    intents = load_submission_intents(client, identifier)

    assert response.status_code == 202
    assert len(intents) == 1
    assert intents[0].request_payload == {
        "operationId": identifier,
        "amount": "125.40",
        "currency": "RUB",
    }


def test_submit_creates_one_processing_event(client: TestClient) -> None:
    identifier = operation_id("submit-event")
    client.post("/operations", json=create_payload(identifier))
    client.post(f"/operations/{identifier}/submit")
    client.post(f"/operations/{identifier}/submit")

    response = client.get(f"/operations/{identifier}/events")

    assert response.status_code == 200
    processing_events = [
        event for event in response.json() if event["type"] == "PROCESSING"
    ]
    assert len(processing_events) == 1
    assert processing_events[0]["eventId"] == 2
    assert processing_events[0]["fromStatus"] == "CREATED"
    assert processing_events[0]["toStatus"] == "PROCESSING"


def test_concurrent_submit_creates_one_intent_and_transition(
    client: TestClient,
) -> None:
    identifier = operation_id("submit-concurrent")
    client.post("/operations", json=create_payload(identifier))

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: client.post(f"/operations/{identifier}/submit"),
                range(2),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 202]
    assert len(load_submission_intents(client, identifier)) == 1

    events_response = client.get(f"/operations/{identifier}/events")
    processing_events = [
        event for event in events_response.json() if event["type"] == "PROCESSING"
    ]
    assert len(processing_events) == 1
