-- Stage 2: ingestion identities and durable dispatch/worker observations.
ALTER TABLE documents ADD COLUMN active_prepare_task_id TEXT;
ALTER TABLE documents ADD COLUMN display_name TEXT;
ALTER TABLE tasks ADD COLUMN stage TEXT;
ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1));
ALTER TABLE tasks ADD COLUMN last_error_json TEXT;
ALTER TABLE attempts ADD COLUMN directory_path TEXT;
ALTER TABLE attempts ADD COLUMN last_progress_at TEXT;
ALTER TABLE outbox ADD COLUMN recovery_quarantined INTEGER NOT NULL DEFAULT 0 CHECK(recovery_quarantined IN (0,1));
ALTER TABLE outbox ADD COLUMN published_worker_id TEXT;
ALTER TABLE outbox ADD COLUMN last_observed_worker_id TEXT;
ALTER TABLE outbox ADD COLUMN last_observed_state_epoch INTEGER;
ALTER TABLE outbox ADD COLUMN last_observed_at REAL;
ALTER TABLE outbox ADD COLUMN idle_seconds REAL NOT NULL DEFAULT 0 CHECK(idle_seconds >= 0);

CREATE TABLE source_roots (
  source_root_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  absolute_path TEXT NOT NULL,
  binding_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(kb_id,absolute_path)
);
CREATE TABLE document_sources (
  source_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  kind TEXT NOT NULL CHECK(kind IN ('FILE','DIRECTORY','URL')),
  source_root_id TEXT REFERENCES source_roots(source_root_id),
  relative_path TEXT,
  source_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(kb_id,source_root_id,relative_path),
  UNIQUE(kb_id,source_key),
  CHECK((kind='DIRECTORY' AND source_root_id IS NOT NULL AND relative_path IS NOT NULL)
     OR (kind='URL' AND source_key IS NOT NULL)
     OR kind='FILE')
);
CREATE TABLE source_observations (
  observation_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES document_sources(source_id),
  raw_version_id TEXT REFERENCES raw_versions(raw_version_id),
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE import_previews (
  preview_id TEXT PRIMARY KEY,
  data_instance_id TEXT NOT NULL,
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  source_root_id TEXT NOT NULL REFERENCES source_roots(source_root_id),
  root_binding_version INTEGER NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  confirmed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE import_batches (
  batch_id TEXT PRIMARY KEY,
  data_instance_id TEXT NOT NULL,
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  request_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  preview_id TEXT REFERENCES import_previews(preview_id),
  expected_bytes INTEGER NOT NULL,
  received_bytes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(data_instance_id,request_key)
);
CREATE TABLE import_items (
  item_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
  ordinal INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('AWAITING_UPLOAD','RECEIVING','ACCEPTED','FAILED')),
  upload_id TEXT,
  task_id TEXT REFERENCES tasks(task_id),
  size_bytes INTEGER,
  content_sha256 TEXT,
  error_json TEXT,
  UNIQUE(batch_id,ordinal)
);
CREATE TABLE task_inputs (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  processing_version_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES document_sources(source_id),
  payload_json TEXT NOT NULL
);
CREATE TABLE task_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(task_id),
  attempt_id TEXT REFERENCES attempts(attempt_id),
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE worker_sessions (
  worker_id TEXT PRIMARY KEY,
  data_instance_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('document-worker','ocr-worker')),
  pid INTEGER NOT NULL,
  process_created_at REAL NOT NULL,
  launch_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('STARTING','IDLE','BUSY','STOPPED')),
  state_epoch INTEGER NOT NULL DEFAULT 0,
  active_task_id TEXT,
  heartbeat_at REAL NOT NULL,
  started_at TEXT NOT NULL
);
CREATE TABLE worker_current (
  role TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL REFERENCES worker_sessions(worker_id)
);
CREATE TABLE publish_observations (
  publish_id TEXT PRIMARY KEY,
  outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id),
  publish_count INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('RESERVED','CONFIRMED','UNKNOWN','NOT_SENT')),
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(outbox_id,publish_count)
);
CREATE TABLE stage_outputs (
  artifact_id TEXT PRIMARY KEY REFERENCES stage_artifacts(artifact_id),
  processing_version_id TEXT NOT NULL,
  stage_input_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  reuse_of_artifact_id TEXT REFERENCES stage_artifacts(artifact_id)
);
CREATE TABLE stage_budgets (
  budget_scope_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_input_sha256 TEXT NOT NULL,
  execution_count INTEGER NOT NULL DEFAULT 0 CHECK(execution_count BETWEEN 0 AND 3),
  PRIMARY KEY(budget_scope_id,stage,stage_input_sha256)
);
CREATE TABLE recovery_choices (
  choice_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  predecessor_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  new_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  recovery_id TEXT,
  observations_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX source_document_idx ON document_sources(document_id);
CREATE INDEX items_batch_idx ON import_items(batch_id,ordinal);
CREATE INDEX task_event_idx ON task_events(task_id,event_id);
CREATE INDEX outputs_reuse_idx ON stage_outputs(processing_version_id,stage_input_sha256);
