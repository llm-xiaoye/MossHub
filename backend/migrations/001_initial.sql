CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
  applied_at TEXT NOT NULL
);

CREATE TABLE system_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  data_instance_id TEXT NOT NULL,
  recovery_id TEXT,
  verification_status TEXT NOT NULL CHECK(verification_status IN ('VERIFYING','VERIFIED','FAILED')),
  dispatch_open INTEGER NOT NULL CHECK(dispatch_open IN (0,1)),
  mode TEXT NOT NULL CHECK(mode IN ('IMPORT','DRAINING','QUERY')),
  updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_bases (
  kb_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  current_generation_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE documents (
  document_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
  current_raw_version_id TEXT,
  current_processing_version_id TEXT,
  removed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE raw_versions (
  raw_version_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
  archive_manifest_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE processing_versions (
  processing_version_id TEXT PRIMARY KEY,
  raw_version_id TEXT NOT NULL REFERENCES raw_versions(raw_version_id),
  config_sha256 TEXT NOT NULL CHECK(length(config_sha256) = 64),
  scope_report_path TEXT,
  scope_report_sha256 TEXT,
  status TEXT NOT NULL CHECK(status IN ('NEW','PROCESSING','REVIEW_REQUIRED','READY','FAILED')),
  created_at TEXT NOT NULL
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  data_instance_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('DOCUMENT','OCR_SUGGESTION')),
  request_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
  status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')),
  current_attempt_id TEXT,
  owner_epoch_counter INTEGER NOT NULL DEFAULT 0 CHECK(owner_epoch_counter >= 0),
  recovery_quarantined INTEGER NOT NULL DEFAULT 0 CHECK(recovery_quarantined IN (0,1)),
  stage_budget_scope_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(data_instance_id, request_key)
);

CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  data_instance_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')),
  owner_epoch INTEGER,
  worker_id TEXT,
  recovery_quarantined INTEGER NOT NULL DEFAULT 0 CHECK(recovery_quarantined IN (0,1)),
  predecessor_attempt_id TEXT REFERENCES attempts(attempt_id),
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  finished_at TEXT,
  CHECK((owner_epoch IS NULL AND worker_id IS NULL) OR (owner_epoch > 0 AND worker_id IS NOT NULL))
);

CREATE TABLE outbox (
  outbox_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
  data_instance_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','PUBLISHING','PUBLISHED','UNKNOWN','CANCELLED')),
  publish_count INTEGER NOT NULL DEFAULT 0 CHECK(publish_count BETWEEN 0 AND 3),
  last_publish_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE provider_attempts (
  provider_attempt_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(task_id),
  attempt_id TEXT REFERENCES attempts(attempt_id),
  request_id TEXT,
  purpose TEXT NOT NULL,
  budget_scope_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('REGISTERED','SENT','SUCCEEDED','FAILED','UNKNOWN')),
  created_at TEXT NOT NULL,
  sent_at TEXT,
  result_at TEXT
);

CREATE TABLE stage_artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  stage TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
  adopted INTEGER NOT NULL DEFAULT 0 CHECK(adopted IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(attempt_id, stage, relative_path)
);

CREATE INDEX tasks_dispatch_idx ON tasks(data_instance_id, status, recovery_quarantined);
CREATE INDEX attempts_task_idx ON attempts(task_id, created_at);
CREATE INDEX outbox_dispatch_idx ON outbox(data_instance_id, state, created_at);
CREATE INDEX provider_budget_idx ON provider_attempts(budget_scope_id, created_at);

