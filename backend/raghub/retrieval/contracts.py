"""Stage 5 accepts an effective knowledge question, never a scope override."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..ingestion.archive import uuid_text


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SessionRequest(Strict):
    kb_id: str

    @field_validator("kb_id")
    @classmethod
    def kb(cls, value):
        uuid_text(value)
        return value


class SearchRequest(Strict):
    effective_original_query: str = Field(min_length=1, max_length=2000)
    rewritten_query: str | None = None

    @field_validator("effective_original_query", "rewritten_query")
    @classmethod
    def nonempty(cls, value):
        if value is not None and not value.replace("\r\n", "\n").replace("\r", "\n").strip():
            raise ValueError("检索问题不能为空")
        return value


def normalized_queries(body):
    original = body.effective_original_query.replace("\r\n", "\n").replace("\r", "\n").strip()
    rewrite = (body.rewritten_query.replace("\r\n", "\n").replace("\r", "\n").strip()
               if body.rewritten_query is not None else None)
    queries = [original]
    if rewrite is not None and rewrite != original:
        queries.append(rewrite)
    rerank_query = queries[-1]
    return {"original": original, "rewrite": rewrite, "unique_queries": queries,
            "rerank_query": rerank_query, "normalization_version": "T57_V1",
            "rerank_template_version": "T64_V2_COMPLETE_QUERY"}
