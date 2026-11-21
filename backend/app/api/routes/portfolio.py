"""Authenticated, owner-only portfolio API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
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


def get_portfolio_owner(
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Authorize the path identity before any portfolio service is called."""
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return current_user


@router.get("/{user_id}/holdings", response_model=HoldingsResponse)
def user_holdings(
    user_id: UUID,
    current_user: Annotated[User, Depends(get_portfolio_owner)],
    session: Annotated[Session, Depends(get_session)],
) -> HoldingsResponse:
    return HoldingsResponse(holdings=get_user_holdings(session, current_user.id))


@router.post(
    "/{user_id}/transactions",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_transaction(
    user_id: UUID,
    data: TransactionCreate,
    current_user: Annotated[User, Depends(get_portfolio_owner)],
    # Authentication starts a read transaction. The write service needs a fresh
    # session so it can own its transaction and retain its user-row lock protocol.
    session: Annotated[Session, Depends(get_session, use_cache=False)],
) -> TransactionResponse:
    try:
        saved = create_transaction(session, current_user.id, data)
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
    current_user: Annotated[User, Depends(get_portfolio_owner)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(gt=0, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> TransactionHistoryResponse:
    try:
        transactions, total = get_user_transactions(session, current_user.id, limit, offset)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
    return TransactionHistoryResponse(
        transactions=[TransactionResponse.model_validate(item) for item in transactions],
        limit=limit,
        offset=offset,
        total=total,
    )
