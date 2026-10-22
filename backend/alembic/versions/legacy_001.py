"""Preserve and apply the original 001 SQL bytes."""
from alembic import context, op
from raghub.schema import apply_legacy

revision = "legacy_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    apply_legacy(op.get_bind(), context.config.attributes["legacy_migrations"], 1)


def downgrade():
    raise RuntimeError("destructive legacy downgrade is not supported")
