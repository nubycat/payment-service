import json

import httpx
import pytest

from app.provider.client import (
    NonRetryableProviderError,
    ProviderClient,
    RetryableProviderError,
)

PROVIDER_URL = "http://provider.test"
OPERATION_ID = "operation-123"
REQUEST_PAYLOAD = {
    "operationId": OPERATION_ID,
    "amount": "125.40",
    "currency": "RUB",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def provider_client(
    handler: httpx.MockTransport,
    http_client: httpx.AsyncClient,
) -> ProviderClient:
    return ProviderClient(
        provider_url=PROVIDER_URL,
        timeout_seconds=1.0,
        http_client=http_client,
    )


@pytest.mark.anyio
async def test_successful_accepted_response() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            202,
            json={"providerPaymentId": "provider-456", "status": "ACCEPTED"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        response = await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)

    assert response.provider_payment_id == "provider-456"
    assert response.status == "ACCEPTED"


@pytest.mark.anyio
async def test_sends_headers_and_saved_payload_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == f"{PROVIDER_URL}/payments"
        assert request.headers["Idempotency-Key"] == OPERATION_ID
        assert request.headers["X-Correlation-ID"] == OPERATION_ID
        assert json.loads(request.content) == REQUEST_PAYLOAD
        return httpx.Response(
            202,
            json={"providerPaymentId": "provider-456", "status": "ACCEPTED"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(RetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_connection_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(RetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_503_is_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(RetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_other_5xx_is_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(RetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_4xx_is_non_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(NonRetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_invalid_json_is_non_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(202, content=b"not-json", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(NonRetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_missing_provider_payment_id_is_non_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            202,
            json={"status": "ACCEPTED"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(NonRetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)


@pytest.mark.anyio
async def test_non_accepted_status_is_non_retryable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            202,
            json={"providerPaymentId": "provider-456", "status": "REJECTED"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = provider_client(transport, http_client)

        with pytest.raises(NonRetryableProviderError):
            await client.submit_payment(OPERATION_ID, REQUEST_PAYLOAD)
