"""Local development portfolio API; requires authorization before public use."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.models import User
from app.schemas.portfolio import HoldingsResponse
from app.services.portfolio import get_user_holdings
from app.schemas.transaction import (
    TransactionCreate,
    TransactionHistoryResponse,
    TransactionResponse,
)
from app.services.transactions import (
    CardNotFoundError,
    InsufficientHoldingsError,
    UserNotFoundError,
    create_transaction,
    get_user_transactions,
)

router = APIRouter(prefix="/users", tags=["portfolio"])


@router.get("/{user_id}/holdings", response_model=HoldingsResponse)
def user_holdings(
    user_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> HoldingsResponse:
    if session.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return HoldingsResponse(holdings=get_user_holdings(session, user_id))


@router.post(
    "/{user_id}/transactions",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_transaction(
    user_id: UUID,
    data: TransactionCreate,
    session: Annotated[Session, Depends(get_session)],
) -> TransactionResponse:
    try:
        saved = create_transaction(session, user_id, data)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
    except CardNotFoundError:
        raise HTTPException(status_code=404, detail="Card not found") from None
    except InsufficientHoldingsError:
        raise HTTPException(status_code=409, detail="Insufficient holdings") from None
    return TransactionResponse.model_validate(saved)


@router.get("/{user_id}/transactions", response_model=TransactionHistoryResponse)
def transaction_history(
    user_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(gt=0, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> TransactionHistoryResponse:
    try:
        transactions, total = get_user_transactions(session, user_id, limit, offset)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
    return TransactionHistoryResponse(
        transactions=[TransactionResponse.model_validate(item) for item in transactions],
        limit=limit,
        offset=offset,
        total=total,
    )
