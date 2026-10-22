"""Typed parsing source locations and validated revision inputs."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Location(Strict):
    file_sha256: str
    granularity: Literal["EXACT", "REGION", "COARSE"]
    basis: str
    source_group: list[dict] | None = None


class PDFLocation(Location):
    kind: Literal["pdf"] = "pdf"
    page: int | None
    bbox: dict | None = None


class MarkdownLocation(Location):
    kind: Literal["md"] = "md"
    line_start: int
    line_end: int
    source_start: int | None = None
    source_end: int | None = None
    resource_sha256: str | None = None
    region: list | None = None
    transform: dict | None = None


class TextLocation(Location):
    kind: Literal["text"] = "text"
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.line_end < self.line_start:
            raise ValueError("text line range is reversed")
        return self


class HTMLLocation(Location):
    kind: Literal["html"] = "html"
    xpath: str
    resource_sha256: str | None = None
    region: list | None = None
    transform: dict | None = None


class DOCXLocation(Location):
    kind: Literal["docx"] = "docx"
    xml_path: str


class XLSXLocation(Location):
    kind: Literal["xlsx"] = "xlsx"
    sheet: str
    cell_range: str


class CSVLocation(Location):
    kind: Literal["csv"] = "csv"
    record: int
    column: int
    physical_line_start: int
    physical_line_end: int


class JSONLocation(Location):
    kind: Literal["json"] = "json"
    pointer: str


class ImageLocation(Location):
    kind: Literal["image"] = "image"
    frame: int
    region: list
    transform: dict


class FileLocation(Location):
    kind: Literal["file"] = "file"
    reason: str


Locator = Annotated[PDFLocation | MarkdownLocation | TextLocation | HTMLLocation | DOCXLocation |
                    XLSXLocation | CSVLocation | JSONLocation | ImageLocation |
                    FileLocation, Field(discriminator="kind")]


class Interval(Strict):
    output_start: int
    output_end: int
    input_start: int
    input_end: int
    input_intervals: list[list[int]]
    precision: Literal["TRANSFORM_TRACE", "FIELD_REFERENCE"]
    operation: str


class FieldSource(Strict):
    field_pointer: str
    original_text: str
    cleaned_text: str
    locator: Locator
    mappings: list[Interval]

    @model_validator(mode="after")
    def covered(self):
        cursor = 0
        for segment in self.mappings:
            if segment.output_start != cursor or segment.output_end <= cursor:
                raise ValueError("output intervals must form complete non-overlapping coverage")
            if not 0 <= segment.input_start <= segment.input_end <= len(self.original_text):
                raise ValueError("input interval outside bound source field")
            if not segment.input_intervals or any(len(pair)!=2 or not 0<=pair[0]<=pair[1]<=len(self.original_text) for pair in segment.input_intervals):
                raise ValueError("invalid traced input intervals")
            if segment.input_start!=min(pair[0] for pair in segment.input_intervals) or segment.input_end!=max(pair[1] for pair in segment.input_intervals):
                raise ValueError("trace bounding interval mismatch")
            cursor = segment.output_end
        if cursor != len(self.cleaned_text):
            raise ValueError("incomplete output coverage")
        return self


class Supplement(Strict):
    schema_version: Literal[1] = 1
    raw_version_id: str
    processing_version_id: str
    original_identity: dict
    adapter: str
    config_sha256: str
    model_lock_sha256: str
    scope: dict
    format_details: dict
    fields: list[FieldSource]
    quality: list[dict]
    images: list[dict]
    mapping_index_units: Literal["PYTHON_UNICODE_CODE_POINT"] = "PYTHON_UNICODE_CODE_POINT"
    interval_semantics: Literal["ZERO_BASED_HALF_OPEN"] = "ZERO_BASED_HALF_OPEN"
