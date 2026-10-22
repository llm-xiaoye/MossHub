"""Stage 5: fixed request scopes, local model ownership and durable retrieval traces."""
from alembic import op

revision = "retrieval_005"
down_revision = "index_004"
branch_labels = None
depends_on = None

DDL = [
    """CREATE TABLE retrieval_requests (
       request_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES generation_sessions(session_id),
       data_instance_id TEXT NOT NULL,request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
       input_json TEXT NOT NULL,snapshot_json TEXT NOT NULL,
       state TEXT NOT NULL CHECK(state IN ('RUNNING','CANCELLING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')),
       owner_pid INTEGER NOT NULL,owner_created_at REAL NOT NULL,
       progress_json TEXT,trace_path TEXT,trace_sha256 TEXT,error_json TEXT,created_at TEXT NOT NULL,finished_at TEXT)""",
    """CREATE TABLE retrieval_model_operations (
       operation_id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES retrieval_requests(request_id),
       registration_json TEXT NOT NULL,observation_json TEXT,updated_at TEXT NOT NULL)""",
    "CREATE INDEX retrieval_requests_active_idx ON retrieval_requests(data_instance_id,state)",
    "CREATE INDEX retrieval_model_operations_request_idx ON retrieval_model_operations(request_id)",
]


def upgrade():
    for statement in DDL:
        op.get_bind().exec_driver_sql(statement)


def downgrade():
    raise RuntimeError("destructive downgrade is not supported; restore a verified backup")
