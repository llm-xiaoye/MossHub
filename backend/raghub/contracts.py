"""Small, strict contracts shared by ingestion, indexing, retrieval, and chat.

The records here describe identities and boundaries. They do not claim that a
document has been parsed, indexed, published, or checked by a person.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class RequestStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class Mode(StrEnum):
    IMPORT = "IMPORT"
    DRAINING = "DRAINING"
    QUERY = "QUERY"


class KnowledgeBaseRef(StrictRecord):
    kb_id: UUID4
    name: str = Field(min_length=1, max_length=160)
    current_generation_id: UUID4 | None = None


class RawVersionRef(StrictRecord):
    raw_version_id: UUID4
    kb_id: UUID4
    document_id: UUID4
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_manifest_path: str


class ProcessingVersionRef(StrictRecord):
    processing_version_id: UUID4
    raw_version_id: UUID4
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_report_path: str | None = None
    scope_report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TaskAttemptRef(StrictRecord):
    data_instance_id: UUID4
    task_id: UUID4
    attempt_id: UUID4
    owner_epoch: int | None = Field(default=None, ge=1)
    publish_count: int = Field(default=0, ge=0, le=3)
    recovery_quarantined: bool = False


class StageArtifactRef(StrictRecord):
    task_id: UUID4
    attempt_id: UUID4
    stage: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adopted: bool = False


class SourceLocator(StrictRecord):
    raw_version_id: UUID4
    source_record_id: str = Field(min_length=1)
    kind: Literal["pdf", "markdown", "html", "docx", "xlsx", "csv", "json", "image"]
    path_or_url: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    sheet_name: str | None = None
    cell_range: str | None = None
    json_pointer: str | None = None
    node_path: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class ParentChildRef(StrictRecord):
    chunk_set_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_version_id: UUID4
    source_record_ids: tuple[str, ...] = Field(min_length=1)


class RetrievalHit(StrictRecord):
    kb_id: UUID4
    generation_id: UUID4
    processing_version_id: UUID4
    chunk_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    route: Literal["dense_original", "dense_rewritten", "bm25_original", "bm25_rewritten"]
    raw_score: float
    rank: int = Field(ge=1)


class EvidenceUnit(StrictRecord):
    support_unit_id: str = Field(min_length=1)
    parent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_version_id: UUID4
    source_record_ids: tuple[str, ...] = Field(min_length=1)
    text: str = Field(min_length=1)


class EvidenceGroup(StrictRecord):
    request_id: UUID4
    kb_id: UUID4
    generation_id: UUID4
    units: tuple[EvidenceUnit, ...] = Field(min_length=1)


class RequestEvidence(StrictRecord):
    """At most eight seed groups in one fully serialized evidence budget.

    The producer must count the actual DeepSeek serialization, including its
    labels and wrappers. These fields validate the boundary; they are not a
    substitute for tokenizing that serialization in the retrieval stage.
    """

    request_id: UUID4
    kb_id: UUID4
    generation_id: UUID4
    groups: tuple[EvidenceGroup, ...] = Field(max_length=8)
    serialized_token_count: int = Field(ge=0, le=8000)

    @model_validator(mode="after")
    def validate_groups(self) -> "RequestEvidence":
        scope = (self.request_id, self.kb_id, self.generation_id)
        if any((group.request_id, group.kb_id, group.generation_id) != scope for group in self.groups):
            raise ValueError("evidence groups must share the request scope")
        return self


class ClarificationResolution(StrictRecord):
    clarification_id: UUID4
    clarification_version: int = Field(ge=1)
    source_turn_id: UUID4
    intent: Literal["keep", "complete", "cancel", "replace"]


class UnderstandingResult(StrictRecord):
    main_action: Literal["retrieve", "respond", "clarify", "feature_limit"]
    status: Literal["standalone", "rewritten", "needs_clarification", "not_applicable"]
    query: str | None
    clarification: str | None
    response_text: str | None
    used_turn_ids: tuple[UUID4, ...]
    used_summary_item_ids: tuple[UUID4, ...]
    clarification_resolution: ClarificationResolution | None

    @model_validator(mode="after")
    def validate_action(self) -> "UnderstandingResult":
        if self.main_action == "retrieve":
            if self.status not in {"standalone", "rewritten"} or not self.query:
                raise ValueError("retrieve requires a query and retrieval status")
            if self.clarification is not None or self.response_text is not None:
                raise ValueError("retrieve cannot include a non-factual response")
        elif self.main_action == "clarify":
            if self.status != "needs_clarification" or not self.clarification:
                raise ValueError("clarify requires one clarification question")
            if self.query is not None or self.response_text is not None:
                raise ValueError("clarify cannot include query or response")
        else:
            if self.status != "not_applicable" or not self.response_text:
                raise ValueError("non-retrieval action requires response_text")
            if self.query is not None or self.clarification is not None:
                raise ValueError("non-retrieval action cannot include query")
        return self


class RequestResult(StrictRecord):
    request_id: UUID4
    status: RequestStatus
    main_action: Literal["retrieve", "respond", "clarify", "feature_limit"] | None
    result_type: Literal[
        "factual_answer", "simple_response", "clarification",
        "cancellation_confirmation", "feature_limit"
    ] | None
    answer_status: AnswerStatus | None

    @model_validator(mode="after")
    def validate_result(self) -> "RequestResult":
        if self.status == RequestStatus.SUCCEEDED:
            if self.main_action is None or self.result_type is None:
                raise ValueError("successful request requires action and result type")
            allowed_results = {
                "retrieve": {"factual_answer"},
                "clarify": {"clarification"},
                "feature_limit": {"feature_limit"},
                "respond": {"simple_response", "cancellation_confirmation"},
            }
            if self.result_type not in allowed_results[self.main_action]:
                raise ValueError("successful result type does not match its action")
            factual = self.result_type == "factual_answer"
            if factual != (self.answer_status is not None):
                raise ValueError("only factual answers have answer_status")
            if factual != (self.main_action == "retrieve"):
                raise ValueError("retrieve must produce a factual result")
        elif self.answer_status is not None:
            raise ValueError("unfinished or failed request has no valid answer_status")
        return self


class RequestScopeSnapshot(StrictRecord):
    request_id: UUID4
    data_instance_id: UUID4
    kb_id: UUID4
    generation_id: UUID4
    release_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    removed_document_ids: tuple[UUID4, ...]
    retrieval_filter: str
    processing_version_ids: tuple[UUID4, ...]
    internal_scope_report_sha256: tuple[str, ...]


class EvaluationScopeSnapshot(StrictRecord):
    evaluation_run_id: UUID4
    question_version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_scope: RequestScopeSnapshot
    frozen_before_answer: bool
    original_question_goal: str = Field(min_length=1)
    expected_behavior: Literal["answered", "partial", "insufficient", "not_applicable"]
