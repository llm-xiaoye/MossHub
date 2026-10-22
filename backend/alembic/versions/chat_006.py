"""Stage 6: full-round chat state, completed history and independent summaries."""
from alembic import op

revision = "chat_006"
down_revision = "retrieval_005"
branch_labels = None
depends_on = None

DDL = [
    """CREATE TABLE chat_conversations (
       conversation_id TEXT PRIMARY KEY REFERENCES generation_sessions(session_id),
       data_instance_id TEXT NOT NULL,request_key TEXT NOT NULL,request_sha256 TEXT NOT NULL,
       summary_id TEXT,created_at TEXT NOT NULL,
       UNIQUE(data_instance_id,request_key))""",
    """CREATE TABLE chat_requests (
       request_id TEXT PRIMARY KEY REFERENCES retrieval_requests(request_id),
       conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
       data_instance_id TEXT NOT NULL,request_key TEXT NOT NULL,request_sha256 TEXT NOT NULL,
       question TEXT NOT NULL,user_message_id TEXT NOT NULL UNIQUE,
       history_boundary INTEGER NOT NULL,awaiting_json TEXT,history_snapshot_json TEXT,
       accepted_at TEXT NOT NULL,started_at TEXT,result_json TEXT,
       UNIQUE(data_instance_id,request_key))""",
    """CREATE TABLE chat_turns (
       turn_id TEXT PRIMARY KEY REFERENCES chat_requests(request_id),
       conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
       ordinal INTEGER NOT NULL,user_message_id TEXT NOT NULL UNIQUE,assistant_message_id TEXT NOT NULL UNIQUE,
       user_text TEXT NOT NULL,assistant_text TEXT NOT NULL,result_type TEXT NOT NULL,result_json TEXT NOT NULL,
       created_at TEXT NOT NULL,UNIQUE(conversation_id,ordinal))""",
    """CREATE TABLE chat_summaries (
       summary_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
       previous_summary_id TEXT,covered_until_ordinal INTEGER NOT NULL,
       value_json TEXT NOT NULL,request_id TEXT NOT NULL REFERENCES chat_requests(request_id),
       created_at TEXT NOT NULL)""",
    """CREATE TABLE chat_clarifications (
       clarification_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
       version INTEGER NOT NULL CHECK(version>0),state TEXT NOT NULL CHECK(state IN ('AWAITING','RESOLVED','CANCELLED','REPLACED')),
       source_turn_id TEXT NOT NULL REFERENCES chat_requests(request_id),
       original_question TEXT NOT NULL,question TEXT NOT NULL,supplements_json TEXT NOT NULL,
       updated_at TEXT NOT NULL)""",
    """CREATE UNIQUE INDEX chat_one_awaiting_idx ON chat_clarifications(conversation_id) WHERE state='AWAITING'""",
    "CREATE INDEX chat_turn_history_idx ON chat_turns(conversation_id,ordinal)",
    "CREATE INDEX chat_requests_conversation_idx ON chat_requests(conversation_id,accepted_at)",
]

def upgrade():
    for statement in DDL:
        op.get_bind().exec_driver_sql(statement)

def downgrade():
    raise RuntimeError("destructive downgrade is not supported; restore a verified backup")

