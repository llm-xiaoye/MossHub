"""Preserve and apply the original 003 SQL bytes."""
from alembic import context, op
from raghub.schema import apply_legacy

revision = "legacy_003"
down_revision = "legacy_002"
branch_labels = None
depends_on = None


def upgrade():
    apply_legacy(op.get_bind(), context.config.attributes["legacy_migrations"], 3)


def downgrade():
    raise RuntimeError("destructive legacy downgrade is not supported")
