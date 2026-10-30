"""Calculate current holdings from a user's card transactions."""

from typing import TypedDict
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Transaction


class Holding(TypedDict):
    card_id: str
    quantity_owned: int


def get_user_holdings(session: Session, user_id: UUID) -> list[Holding]:
    """Return positive net quantities per card, ordered by card ID."""
    net_quantity = func.sum(
        case(
            (Transaction.type == "BUY", Transaction.quantity),
            (Transaction.type == "SELL", -Transaction.quantity),
            else_=0,
        )
    )
    statement = (
        select(Transaction.card_id, net_quantity.label("quantity_owned"))
        .where(Transaction.user_id == user_id)
        .group_by(Transaction.card_id)
        .having(net_quantity > 0)
        .order_by(Transaction.card_id)
    )
    return [
        {"card_id": row.card_id, "quantity_owned": row.quantity_owned}
        for row in session.execute(statement)
    ]
