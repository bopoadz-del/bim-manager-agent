"""The append-only event log, and the only sanctioned way to change state.

Every transition goes through :func:`transition`. That is not ceremony: the
acceptance criteria ask the ledger to show *the sequence* by which a boundary
proposal was rejected and re-attempted (A2) and by which a verified proposal
returned to ``proposed`` after a neighbour committed (A5). A state field written
directly by some other module is a step that sequence will not contain, and the
absence is invisible until someone needs the history and finds a hole in it.

``record`` exists for things that are not state changes but still belong in the
history -- an ingest, a monitor verdict, a rebase enqueue.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerEvent


class IllegalTransition(Exception):
    """Raised when a caller asks for a transition the vocabulary forbids."""


def record(
    session: Session,
    *,
    entity: str,
    entity_id: str,
    to_state: str | None = None,
    from_state: str | None = None,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
) -> LedgerEvent:
    event = LedgerEvent(
        entity=entity,
        entity_id=entity_id,
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        payload=payload or {},
    )
    session.add(event)
    return event


def transition(
    session: Session,
    obj: Any,
    *,
    entity: str,
    to_state: str,
    allowed: tuple[str, ...],
    actor: str = "system",
    payload: dict[str, Any] | None = None,
    field: str | None = None,
) -> LedgerEvent:
    """Move ``obj`` to ``to_state`` and write the matching ledger row.

    The write and the event are one call so they cannot drift apart. An unknown
    target raises rather than being coerced to something plausible -- a state
    machine that accepts a typo silently is a state machine that will one day
    report a clash as merged because someone wrote "merge".

    The state column is named by the model itself (``LEDGER_FIELD``), because the
    schema calls it ``status`` on a zone and ``state`` on a clash. Guessing would
    work for one and raise for the other, mid-run.
    """
    field_name: str = field or str(getattr(type(obj), "LEDGER_FIELD", "state"))
    if to_state not in allowed:
        raise IllegalTransition(
            f"{entity}.{field_name} cannot become {to_state!r}; allowed: {', '.join(allowed)}"
        )
    previous = getattr(obj, field_name)
    setattr(obj, field_name, to_state)
    return record(
        session,
        entity=entity,
        entity_id=obj.id,
        from_state=previous,
        to_state=to_state,
        actor=actor,
        payload=payload,
    )


def history(session: Session, entity: str, entity_id: str) -> list[LedgerEvent]:
    stmt = (
        select(LedgerEvent)
        .where(LedgerEvent.entity == entity, LedgerEvent.entity_id == entity_id)
        .order_by(LedgerEvent.seq)
    )
    return list(session.execute(stmt).scalars())
