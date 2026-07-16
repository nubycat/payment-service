from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings, get_settings


class ProviderClientError(Exception):
    pass


class RetryableProviderError(ProviderClientError):
    pass


class NonRetryableProviderError(ProviderClientError):
    pass


class ProviderAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider_payment_id: str = Field(alias="providerPaymentId")
    status: Literal["ACCEPTED"]

    @field_validator("provider_payment_id")
    @classmethod
    def validate_provider_payment_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("providerPaymentId must not be blank")
        return value


class ProviderClient:
    def __init__(
        self,
        provider_url: str,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._payments_url = f"{provider_url.rstrip('/')}/payments"
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_http_client = http_client is None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ProviderClient":
        current_settings = settings or get_settings()
        return cls(
            provider_url=current_settings.provider_url,
            timeout_seconds=current_settings.provider_timeout_seconds,
        )

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def submit_payment(
        self,
        operation_id: str,
        request_payload: Mapping[str, Any],
    ) -> ProviderAcceptedResponse:
        try:
            response = await self._http_client.post(
                self._payments_url,
                headers={
                    "Idempotency-Key": operation_id,
                    "X-Correlation-ID": operation_id,
                },
                json=request_payload,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise RetryableProviderError("Provider request failed") from error

        if 500 <= response.status_code <= 599:
            raise RetryableProviderError(
                f"Provider returned HTTP {response.status_code}"
            )
        if response.status_code != httpx.codes.ACCEPTED:
            raise NonRetryableProviderError(
                f"Provider returned HTTP {response.status_code}"
            )

        try:
            return ProviderAcceptedResponse.model_validate(response.json())
        except ValueError as error:
            raise NonRetryableProviderError("Provider returned an invalid response") from error
