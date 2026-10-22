"""Durable, bounded Obsidian import-to-answer workflows."""
from alembic import op

revision = "workflow_008"
down_revision = "obsidian_007"
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql("""CREATE TABLE import_workflows (
      workflow_id TEXT PRIMARY KEY,
      data_instance_id TEXT NOT NULL,
      kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
      request_key TEXT NOT NULL,
      request_sha256 TEXT NOT NULL,
      preview_id TEXT NOT NULL REFERENCES import_previews(preview_id),
      state TEXT NOT NULL,
      data_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(data_instance_id,request_key),
      UNIQUE(data_instance_id,preview_id))""")
    op.get_bind().exec_driver_sql("""CREATE UNIQUE INDEX one_active_import_workflow
      ON import_workflows(data_instance_id)
      WHERE state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED')""")

def downgrade():
    raise RuntimeError("restore a verified backup instead of destructive downgrade")
