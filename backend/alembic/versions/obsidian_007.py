"""Exact normal-file input claims; original/source history is unchanged."""
from alembic import op

revision = "obsidian_007"
down_revision = "chat_006"
branch_labels = None
depends_on = None

DDL = [
    """CREATE TABLE file_input_claims (
       data_instance_id TEXT NOT NULL,
       kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
       fingerprint TEXT NOT NULL, input_json TEXT NOT NULL,
       document_id TEXT NOT NULL REFERENCES documents(document_id),
       source_id TEXT NOT NULL REFERENCES document_sources(source_id),
       task_id TEXT NOT NULL REFERENCES tasks(task_id),
       created_at TEXT NOT NULL,
       PRIMARY KEY(data_instance_id,kb_id,fingerprint))""",
    "CREATE INDEX file_claim_document_idx ON file_input_claims(document_id)",
]

def upgrade():
    for statement in DDL:
        op.get_bind().exec_driver_sql(statement)

def downgrade():
    raise RuntimeError("destructive downgrade is not supported; restore a verified backup")

