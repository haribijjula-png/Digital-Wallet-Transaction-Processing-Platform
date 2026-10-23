import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Money: strictly positive, max 2 decimal places. Serialised as strings ("800.00") to avoid float drift.
Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2, examples=["100.00"])]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$", examples=["INR"])]

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail


class MessageResponse(BaseModel):
    success: bool = True
    message: str


class Timestamped(ORMModel):
    created_at: datetime
    updated_at: datetime | None = None


UUIDStr = uuid.UUID
