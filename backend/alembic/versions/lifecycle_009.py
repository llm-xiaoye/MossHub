"""Simple intake continuations and durable document deletion."""
from alembic import op
revision = "lifecycle_009"
down_revision = "workflow_008"
branch_labels = None
depends_on = None

def upgrade():
    db = op.get_bind()
    db.exec_driver_sql("DROP INDEX one_active_import_workflow")
    db.exec_driver_sql("ALTER TABLE import_workflows RENAME TO import_workflows_008")
    db.exec_driver_sql("""CREATE TABLE import_workflows (
      workflow_id TEXT PRIMARY KEY,data_instance_id TEXT NOT NULL,
      kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),request_key TEXT NOT NULL,
      request_sha256 TEXT NOT NULL,preview_id TEXT REFERENCES import_previews(preview_id),
      state TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      UNIQUE(data_instance_id,request_key),UNIQUE(data_instance_id,preview_id))""")
    db.exec_driver_sql("INSERT INTO import_workflows SELECT * FROM import_workflows_008")
    db.exec_driver_sql("DROP TABLE import_workflows_008")
    db.exec_driver_sql("""CREATE UNIQUE INDEX one_active_import_workflow ON import_workflows(data_instance_id)
      WHERE state NOT IN ('COMPLETE','PARTIAL_COMPLETE','CANCELLED')""")
    db.exec_driver_sql("""CREATE TABLE document_deletions (
      document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
      kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),data_instance_id TEXT NOT NULL,
      state TEXT NOT NULL,data_json TEXT NOT NULL,error_text TEXT,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")

def downgrade():
    raise RuntimeError("restore a verified backup instead of destructive downgrade")

