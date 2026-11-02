"""v0.3-A: market observation versions are identified by sequence, not content.

The 0004 unique constraints included ``content_hash``, which made a source reverting to an
earlier value (A -> B -> A) impossible to record: the revert was silently discarded and the
current / later point-in-time views kept serving B. Versions are now unique by
(observation key, version); duplicate detection happens in ``MarketStore`` against the latest
version.

Revision ID: 0005_market_version_identity
Revises: 0004_market_data
"""

from __future__ import annotations

from alembic import op

revision = "0005_market_version_identity"
down_revision = "0004_market_data"
branch_labels = None
depends_on = None

_BAR_KEY = ["instrument_id", "provider", "frequency", "adjustment", "session_date"]


def upgrade() -> None:
    with op.batch_alter_table("market_price_bars") as b:
        b.drop_constraint("uq_market_bar_version", type_="unique")
        b.create_unique_constraint("uq_market_bar_seq", [*_BAR_KEY, "version"])
    with op.batch_alter_table("market_corporate_actions") as b:
        b.drop_constraint("uq_market_action_version", type_="unique")
        b.create_unique_constraint("uq_market_action_seq", ["provider", "action_id", "version"])
    with op.batch_alter_table("market_yield_observations") as b:
        b.drop_constraint("uq_market_yield_version", type_="unique")
        b.create_unique_constraint("uq_market_yield_seq", ["series_id", "observation_date", "version"])


def downgrade() -> None:
    # Fails if a revert (repeated content_hash) has been stored, so data is never dropped silently.
    with op.batch_alter_table("market_yield_observations") as b:
        b.drop_constraint("uq_market_yield_seq", type_="unique")
        b.create_unique_constraint("uq_market_yield_version", ["series_id", "observation_date", "content_hash"])
    with op.batch_alter_table("market_corporate_actions") as b:
        b.drop_constraint("uq_market_action_seq", type_="unique")
        b.create_unique_constraint("uq_market_action_version", ["provider", "action_id", "content_hash"])
    with op.batch_alter_table("market_price_bars") as b:
        b.drop_constraint("uq_market_bar_seq", type_="unique")
        b.create_unique_constraint("uq_market_bar_version", [*_BAR_KEY, "content_hash"])
