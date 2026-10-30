"""Portfolio API response models."""

from pydantic import BaseModel


class HoldingResponse(BaseModel):
    card_id: str
    quantity_owned: int


class HoldingsResponse(BaseModel):
    holdings: list[HoldingResponse]
