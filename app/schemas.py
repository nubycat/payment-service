import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from app.enums import OperationStatus, ReceiptResult

_AMOUNT_PATTERN = re.compile(r"^\d+(?:\.\d{1,2})?$")


class OperationCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    operation_id: str = Field(alias="operationId", min_length=1, max_length=128)
    amount: Decimal
    currency: str
    description: str | None = Field(default=None, max_length=500)

    @field_validator("operation_id")
    @classmethod
    def validate_operation_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operationId must not be blank")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        if not isinstance(value, str) or _AMOUNT_PATTERN.fullmatch(value) is None:
            raise ValueError("amount must be a positive decimal string with at most two decimals")
        try:
            amount = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("amount must be a valid decimal string") from error
        if amount <= 0:
            raise ValueError("amount must be greater than zero")
        return amount

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if value != "RUB":
            raise ValueError("currency must be RUB")
        return value


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    operation_id: str = Field(alias="operationId")
    amount: Decimal
    currency: str
    description: str | None
    status: OperationStatus
    provider_payment_id: str | None = Field(alias="providerPaymentId")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")), "f")


class OperationEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    event_id: int = Field(alias="eventId")
    event_type: OperationStatus = Field(alias="type")
    from_status: OperationStatus | None = Field(alias="fromStatus")
    to_status: OperationStatus = Field(alias="toStatus")
    message: str
    occurred_at: datetime = Field(alias="occurredAt")


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    provider_payment_id: str = Field(alias="providerPaymentId", min_length=1)
    operation_id: str = Field(alias="operationId", min_length=1, max_length=128)
    result: ReceiptResult
    message: str
    occurred_at: AwareDatetime = Field(alias="occurredAt")

    @field_validator("operation_id", "provider_payment_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value
