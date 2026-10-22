-- Stage 3: prepared parsing versions, full-JSON revisions and OCR suggestion records.
CREATE TABLE processing_configs (
  processing_version_id TEXT PRIMARY KEY REFERENCES processing_versions(processing_version_id),
  config_json TEXT NOT NULL,
  parent_processing_version_id TEXT REFERENCES processing_versions(processing_version_id),
  origin_kind TEXT NOT NULL CHECK(origin_kind IN ('AUTOMATIC','MANUAL_JSON')),
  revision_id TEXT,
  prepare_task_id TEXT NOT NULL REFERENCES tasks(task_id)
);
CREATE TABLE processing_results (
  processing_version_id TEXT PRIMARY KEY REFERENCES processing_versions(processing_version_id),
  artifact_id TEXT NOT NULL REFERENCES stage_artifacts(artifact_id),
  bundle_path TEXT NOT NULL,
  bundle_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE revision_previews (
  preview_id TEXT PRIMARY KEY,
  data_instance_id TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  base_processing_version_id TEXT NOT NULL REFERENCES processing_versions(processing_version_id),
  base_bundle_sha256 TEXT NOT NULL,
  revision_path TEXT NOT NULL,
  revision_sha256 TEXT NOT NULL,
  difference_json TEXT NOT NULL,
  submitted_processing_version_id TEXT,
  human_decision_id TEXT,
  created_at TEXT NOT NULL,
  review_json TEXT NOT NULL,
  review_sha256 TEXT NOT NULL,
  preview_sha256 TEXT NOT NULL,
  origin_decision_id TEXT UNIQUE REFERENCES human_decisions(decision_id)
);
CREATE TABLE human_decisions (
  decision_id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('REVISION','OCR_SUGGESTION')),
  target_id TEXT NOT NULL,
  processing_version_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('ACCEPT','REJECT','MODIFY_ACCEPT')),
  source TEXT NOT NULL,
  note TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE ocr_suggestion_requests (
  request_id TEXT PRIMARY KEY,
  task_id TEXT UNIQUE NOT NULL REFERENCES tasks(task_id),
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  processing_version_id TEXT NOT NULL REFERENCES processing_versions(processing_version_id),
  input_path TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE ocr_suggestion_results (
  result_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES ocr_suggestion_requests(request_id),
  provider_attempt_id TEXT NOT NULL REFERENCES provider_attempts(provider_attempt_id),
  result_path TEXT NOT NULL,
  result_sha256 TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK(disposition IN ('AVAILABLE','DIAGNOSTIC','UNKNOWN')),
  created_at TEXT NOT NULL
);
CREATE INDEX processing_config_task_idx ON processing_configs(prepare_task_id);
CREATE INDEX ocr_request_processing_idx ON ocr_suggestion_requests(processing_version_id);
