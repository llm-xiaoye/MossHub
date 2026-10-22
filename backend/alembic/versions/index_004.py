"""Stage 4: immutable chunks, verified vector cache and generation publication."""
from alembic import op

revision = "index_004"
down_revision = "legacy_003"
branch_labels = None
depends_on = None

DDL = [
    """CREATE TABLE model_profiles (profile_id TEXT PRIMARY KEY CHECK(length(profile_id)=64),
       kind TEXT NOT NULL CHECK(kind IN ('EMBEDDING_SPACE','MODEL_EXECUTION_PROFILE','RERANKER_PROFILE')),
       config_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE vector_cache (cache_key TEXT PRIMARY KEY CHECK(length(cache_key)=64),
       embedding_space_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       execution_profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       input_kind TEXT NOT NULL CHECK(input_kind IN ('document_standard','document_extended')),
       input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),dimension INTEGER NOT NULL CHECK(dimension=1024),
       vector_path TEXT NOT NULL UNIQUE,vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256)=64),
       size_bytes INTEGER NOT NULL CHECK(size_bytes>0),norm REAL NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE chunk_sets (chunk_set_id TEXT PRIMARY KEY CHECK(length(chunk_set_id)=64),
       processing_version_id TEXT NOT NULL REFERENCES processing_versions(processing_version_id),
       embedding_space_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       rules_sha256 TEXT NOT NULL CHECK(length(rules_sha256)=64),bundle_sha256 TEXT NOT NULL CHECK(length(bundle_sha256)=64),
       manifest_path TEXT NOT NULL UNIQUE,manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
       parent_count INTEGER NOT NULL CHECK(parent_count>0),child_count INTEGER NOT NULL CHECK(child_count>0),
       created_at TEXT NOT NULL)""",
    """CREATE TABLE chunk_vectors (chunk_set_id TEXT NOT NULL REFERENCES chunk_sets(chunk_set_id),
       chunk_id TEXT NOT NULL CHECK(length(chunk_id)=64),cache_key TEXT NOT NULL REFERENCES vector_cache(cache_key),
       execution_profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       chunk_ordinal INTEGER NOT NULL CHECK(chunk_ordinal>=0),
       PRIMARY KEY(chunk_set_id,chunk_id,execution_profile_id),
       UNIQUE(chunk_set_id,chunk_ordinal,execution_profile_id))""",
    """CREATE TABLE generations (generation_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
       task_id TEXT NOT NULL REFERENCES tasks(task_id),attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
       state TEXT NOT NULL CHECK(state IN ('BUILDING','CHECKING','READY','CURRENT','RETIRED','FAILED','ABORTED')),
       collection_name TEXT NOT NULL UNIQUE,embedding_space_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       execution_profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
       input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),expected_previous_generation_id TEXT,
       manifest_path TEXT,manifest_sha256 TEXT,last_error_json TEXT,created_at TEXT NOT NULL,ready_at TEXT,published_at TEXT,
       cleanup_state TEXT NOT NULL DEFAULT 'NONE' CHECK(cleanup_state IN ('NONE','RUNNING','SUCCEEDED','FAILED')),
       collection_removed_at TEXT,UNIQUE(task_id,attempt_id))""",
    """CREATE TABLE generation_documents (generation_id TEXT NOT NULL REFERENCES generations(generation_id),
       document_id TEXT NOT NULL REFERENCES documents(document_id),
       raw_version_id TEXT NOT NULL REFERENCES raw_versions(raw_version_id),
       processing_version_id TEXT NOT NULL REFERENCES processing_versions(processing_version_id),
       chunk_set_id TEXT REFERENCES chunk_sets(chunk_set_id),PRIMARY KEY(generation_id,document_id))""",
    """CREATE TABLE generation_inputs (task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
       kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),payload_json TEXT NOT NULL,
       input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),created_at TEXT NOT NULL)""",
    """CREATE TABLE removal_epochs (kb_id TEXT PRIMARY KEY REFERENCES knowledge_bases(kb_id),
       revision INTEGER NOT NULL CHECK(revision>=0),updated_at TEXT NOT NULL)""",
    """CREATE TABLE removal_events (event_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
       document_id TEXT NOT NULL REFERENCES documents(document_id),revision INTEGER NOT NULL CHECK(revision>0),
       removed_at TEXT NOT NULL,UNIQUE(kb_id,revision))""",
    """CREATE TABLE generation_readers (request_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL REFERENCES generations(generation_id),
       data_instance_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
       created_at TEXT NOT NULL,closed_at TEXT)""",
    """CREATE TABLE generation_sessions (session_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
       generation_id TEXT NOT NULL REFERENCES generations(generation_id),read_only INTEGER NOT NULL CHECK(read_only IN (0,1)),
       created_at TEXT NOT NULL)""",
    """CREATE TABLE generation_cleanup_previews (preview_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
       data_instance_id TEXT NOT NULL,snapshot_json TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
       created_at TEXT NOT NULL,consumed_at TEXT)""",
    "CREATE INDEX generations_kb_publication_idx ON generations(kb_id,published_at)",
    "CREATE INDEX generation_inputs_kb_idx ON generation_inputs(kb_id)",
    "CREATE INDEX generation_readers_active_idx ON generation_readers(generation_id,state)",
]


def upgrade():
    connection = op.get_bind()
    for statement in DDL:
        connection.exec_driver_sql(statement)


def downgrade():
    raise RuntimeError("destructive downgrade is not supported; restore a verified backup")
