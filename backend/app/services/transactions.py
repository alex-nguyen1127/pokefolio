"""Transaction creation and user-scoped history queries."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Card, Transaction, User
from app.schemas.transaction import TransactionCreate
from app.services.portfolio import get_user_holdings


class UserNotFoundError(Exception):
    pass


class CardNotFoundError(Exception):
    pass


class InsufficientHoldingsError(Exception):
    pass


def get_user_transactions(
    session: Session, user_id: UUID, limit: int, offset: int
) -> tuple[list[Transaction], int]:
    """Return a newest-first page and the user's total transaction count."""
    if session.get(User, user_id) is None:
        raise UserNotFoundError
    total = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id)
    )
    transactions = session.scalars(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(
            Transaction.transacted_at.desc(),
            Transaction.created_at.desc(),
            Transaction.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return list(transactions), total


def create_transaction(
    session: Session, user_id: UUID, data: TransactionCreate
) -> Transaction:
    """Lock, validate, and save atomically; caller must supply an idle session.

    All BUY and SELL writes must use this lock protocol. PostgreSQL's default
    READ COMMITTED isolation lets a waiting writer see the prior writer's commit
    in the subsequent holdings query. SQLite does not implement this row lock.
    """
    with session.begin():
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise UserNotFoundError
        if session.get(Card, data.card_id) is None:
            raise CardNotFoundError
        if data.type == "SELL":
            available = next(
                (holding["quantity_owned"] for holding in get_user_holdings(session, user_id)
                 if holding["card_id"] == data.card_id),
                0,
            )
            if data.quantity > available:
                raise InsufficientHoldingsError
        transaction = Transaction(user_id=user_id, **data.model_dump())
        session.add(transaction)
        session.flush()
        session.refresh(transaction)
        # Keep loaded response fields available even for expire_on_commit=True
        # callers. Flush/refresh failures still roll back the enclosing block.
        session.expunge(transaction)
    return transaction
