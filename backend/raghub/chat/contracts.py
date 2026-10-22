"""Exact cloud and HTTP contracts; drafts are never formal results."""
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

class Question(Strict):
    question: str = Field(min_length=1, max_length=2000)
    @model_validator(mode="after")
    def valid(self):
        if not self.question.strip() or "\x00" in self.question:
            raise ValueError("question must contain text without NUL")
        return self

class ConversationCreate(Strict):
    kb_id: str

class Resolution(Strict):
    clarification_id: str
    clarification_version: int = Field(ge=1)
    source_turn_id: str
    intent: Literal["keep","complete","cancel","replace"]

class Understanding(Strict):
    main_action: Literal["retrieve","clarify","respond","feature_limit"]
    status: Literal["standalone","rewritten","needs_clarification","not_applicable"]
    query: str | None = Field(min_length=1, max_length=2000)
    clarification: str | None = Field(min_length=1, max_length=200)
    response_text: str | None = Field(min_length=1, max_length=2000)
    used_turn_ids: list[str] = Field(max_length=3)
    used_summary_item_ids: list[str] = Field(max_length=3)
    clarification_resolution: Resolution | None
    @model_validator(mode="after")
    def combination(self):
        if len(set(self.used_turn_ids)) != len(self.used_turn_ids) or len(set(self.used_summary_item_ids)) != len(self.used_summary_item_ids):
            raise ValueError("duplicate history references")
        if self.main_action == "retrieve":
            valid = self.status in {"standalone","rewritten"} and self.query is not None and self.clarification is None and self.response_text is None
        elif self.main_action == "clarify":
            valid = self.status == "needs_clarification" and self.query is None and self.clarification is not None and self.response_text is None
        else:
            valid = self.status == "not_applicable" and self.query is None and self.clarification is None and self.response_text is not None
        if not valid:
            raise ValueError("understanding action/status/value combination differs")
        return self

class Paragraph(Strict):
    text: str = Field(min_length=1)
    support_unit_ids: list[str] = Field(min_length=1, max_length=6)
    @model_validator(mode="after")
    def units(self):
        if not self.text.strip() or len(set(self.support_unit_ids)) != len(self.support_unit_ids):
            raise ValueError("empty paragraph or repeated support units")
        return self

class Answer(Strict):
    status: Literal["answered","partial","insufficient"]
    paragraphs: list[Paragraph]
    limitations: list[str]
    @model_validator(mode="after")
    def combination(self):
        if any(not value.strip() for value in self.limitations):
            raise ValueError("limitations must contain text")
        if self.status == "answered" and not self.paragraphs:
            raise ValueError("answered requires factual paragraphs")
        if self.status == "partial" and (not self.paragraphs or not self.limitations):
            raise ValueError("partial requires facts and limitations")
        if self.status == "insufficient" and (self.paragraphs or not self.limitations):
            raise ValueError("insufficient has only limitations")
        return self

class SummaryItem(Strict):
    item_id: str
    category: Literal["topic","entity","constraint","correction","open_question","dialogue_note"]
    content: str = Field(min_length=1)
    source_message_ids: list[str] = Field(min_length=1)
    proposer: Literal["user","assistant","third_party","unknown"]
    scope: str = Field(min_length=1)
    status: str | None
    confirmation_message_id: str | None
    supersedes_item_ids: list[str]
    superseded_by_item_id: str | None
    @model_validator(mode="after")
    def status_domain(self):
        allowed = ({"suggested","conditional","decided","superseded"} if self.category in {"constraint","correction"}
                   else {"unresolved","resolved","cancelled","superseded"} if self.category == "open_question" else {None})
        if self.status not in allowed or len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("summary category/status/source combination differs")
        if self.status == "decided" and self.confirmation_message_id is None:
            raise ValueError("decided requires an actual user confirmation")
        return self

class Summary(Strict):
    items: list[SummaryItem]

def normalized_question(value):
    return value.replace("\r\n","\n").replace("\r","\n").strip()

def explicit_control(question):
    """Only complete, unambiguous sentences match; quoted/additional text falls through."""
    greeting = re.fullmatch(r"(?:你好|您好|谢谢|谢谢你|再见)[。！! ]*", question)
    cancel = re.fullmatch(r"(?:取消|不问了|取消(?:刚才|上一个|这次)的问题)[。！! ]*", question)
    return "greeting" if greeting else "cancel" if cancel else None

