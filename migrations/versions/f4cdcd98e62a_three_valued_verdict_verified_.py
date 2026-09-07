"""three-valued verdict: verified_conditional

Adds the state that sits between "verified" and "rejected": every monitor
accepted the move, and at least one of their checks could not be answered by
this model.

Two things autogenerate got wrong and are corrected here:

* ``unprovable_checks`` needs a server default. Adding a NOT NULL column with no
  default succeeds on an empty SQLite file and fails on a Postgres table that
  already holds proposals — which is every deployed one.
* The named CHECK constraints have to be dropped and recreated. Widening a
  VARCHAR does not widen the vocabulary the constraint allows, so on Postgres the
  new state would be rejected at write time by a constraint this migration
  appeared to have updated.

Revision ID: f4cdcd98e62a
Revises: e9587aa1cc01
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4cdcd98e62a"
down_revision = "e9587aa1cc01"
branch_labels = None
depends_on = None

CLASH_STATES_NEW = (
    "open, proposed, verified, verified_conditional, approved, merged, "
    "resolved, regressed, escalated"
)
CLASH_STATES_OLD = "open, proposed, verified, approved, merged, resolved, regressed, escalated"
PROPOSAL_VERDICTS_NEW = (
    "pending, verified, verified_conditional, rejected, flagged_unsourced, "
    "escalated, approved, superseded"
)
PROPOSAL_VERDICTS_OLD = (
    "pending, verified, rejected, flagged_unsourced, escalated, approved, superseded"
)


def _in_clause(column: str, values: str) -> str:
    return f"{column} in (" + ", ".join(f"'{v.strip()}'" for v in values.split(",")) + ")"


def upgrade() -> None:
    with op.batch_alter_table("clash", schema=None) as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=sa.VARCHAR(length=20),
            type_=sa.String(length=30),
            existing_nullable=False,
        )
        batch_op.drop_constraint("ck_clash_state", type_="check")
        batch_op.create_check_constraint(
            "ck_clash_state", _in_clause("state", CLASH_STATES_NEW)
        )

    with op.batch_alter_table("proposal", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "unprovable_checks",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.alter_column(
            "verdict",
            existing_type=sa.VARCHAR(length=20),
            type_=sa.String(length=30),
            existing_nullable=False,
        )
        batch_op.drop_constraint("ck_proposal_verdict", type_="check")
        batch_op.create_check_constraint(
            "ck_proposal_verdict", _in_clause("verdict", PROPOSAL_VERDICTS_NEW)
        )


def downgrade() -> None:
    # Rows already holding the new state cannot survive the old constraint. They
    # become "escalated", not "verified": a conditional verification is a
    # proposal with unanswered checks, and quietly promoting it to a full
    # verification on the way down would assert something no monitor ever said.
    op.execute("update clash set state = 'escalated' where state = 'verified_conditional'")
    op.execute(
        "update proposal set verdict = 'escalated' where verdict = 'verified_conditional'"
    )

    with op.batch_alter_table("proposal", schema=None) as batch_op:
        batch_op.drop_constraint("ck_proposal_verdict", type_="check")
        batch_op.create_check_constraint(
            "ck_proposal_verdict", _in_clause("verdict", PROPOSAL_VERDICTS_OLD)
        )
        batch_op.alter_column(
            "verdict",
            existing_type=sa.String(length=30),
            type_=sa.VARCHAR(length=20),
            existing_nullable=False,
        )
        batch_op.drop_column("unprovable_checks")

    with op.batch_alter_table("clash", schema=None) as batch_op:
        batch_op.drop_constraint("ck_clash_state", type_="check")
        batch_op.create_check_constraint(
            "ck_clash_state", _in_clause("state", CLASH_STATES_OLD)
        )
        batch_op.alter_column(
            "state",
            existing_type=sa.String(length=30),
            type_=sa.VARCHAR(length=20),
            existing_nullable=False,
        )
