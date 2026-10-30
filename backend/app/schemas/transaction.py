"""Transaction input validation and saved transaction responses."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionCreate(BaseModel):
    card_id: str = Field(min_length=1, max_length=50)
    type: Literal["BUY", "SELL"]
    quantity: int = Field(strict=True, gt=0, le=2_147_483_647)
    price_per_unit: Decimal = Field(
        ge=0, le=Decimal("9999999999.99"), max_digits=12, decimal_places=2,
        allow_inf_nan=False,
    )

    @field_validator("price_per_unit")
    @classmethod
    def reject_extra_decimal_places(cls, value: Decimal) -> Decimal:
        # Also reject extra trailing zeros rather than normalizing precision.
        if value.as_tuple().exponent < -2:
            raise ValueError("price_per_unit must have at most two decimal places")
        return value


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    card_id: str
    type: Literal["BUY", "SELL"]
    quantity: int
    price_per_unit: Decimal
    transacted_at: datetime
    created_at: datetime


class TransactionHistoryResponse(BaseModel):
    transactions: list[TransactionResponse]
    limit: int
    offset: int
    total: int
