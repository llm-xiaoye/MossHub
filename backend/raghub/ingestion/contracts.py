"""Validated metadata; client paths never choose archive destinations."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, UUID4, field_validator, model_validator

from ..contracts import StrictRecord

MIB = 1024 * 1024
FILE_LIMIT = 50 * MIB
BATCH_LIMIT = 500 * MIB
BATCH_FILES = 50
MULTIPART_ALLOWANCE = 64 * 1024
SUFFIXES = frozenset({
    ".pdf", ".md", ".txt", ".html", ".htm", ".docx", ".xlsx", ".csv", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".svg",
})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".svg"})


def relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("relative path must use nonempty POSIX components")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative path cannot escape or contain empty/dot components")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise ValueError("control characters are not allowed in a path")
    return value


def url_source_key(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("a public HTTP or HTTPS URL is required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not allowed")
    host = parts.hostname.lower()
    port = parts.port
    if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
        raise ValueError("only standard HTTP/HTTPS ports are supported")
    return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, parts.fragment))


class Provenance(StrictRecord):
    nature: Literal["ORIGINAL", "DERIVED", "CONSTRUCTED"] = "ORIGINAL"
    origin_url: str | None = Field(default=None, max_length=8192)
    origin_local_path: str | None = Field(default=None, max_length=8192)
    source_revision: str | None = Field(default=None, max_length=500)
    source_date: str | None = Field(default=None, max_length=100)
    license: str | None = Field(default=None, max_length=1000)
    derivation: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def derived_has_explanation(self):
        if self.nature in {"DERIVED", "CONSTRUCTED"} and not self.derivation:
            raise ValueError("derived/constructed material requires an explanation")
        return self


class FileItem(StrictRecord):
    filename: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(ge=0, le=FILE_LIMIT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str | None = None
    document_id: UUID4 | None = None
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value):
        relative_path(value)
        if "/" in value or PurePosixPath(value).suffix.lower() not in SUFFIXES:
            raise ValueError("unsupported file suffix or filename")
        return value

    @field_validator("relative_path")
    @classmethod
    def validate_relative(cls, value):
        return relative_path(value) if value is not None else None


class BatchRequest(StrictRecord):
    kb_id: UUID4
    items: list[FileItem] = Field(min_length=1, max_length=BATCH_FILES)
    preview_id: UUID4 | None = None
    preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_batch(self):
        if sum(item.size_bytes for item in self.items) > BATCH_LIMIT:
            raise ValueError("batch exceeds 500 MiB")
        if (self.preview_id is None) != (self.preview_sha256 is None):
            raise ValueError("preview identity and digest must be provided together")
        paths = [item.relative_path for item in self.items if item.relative_path is not None]
        if len(paths) != len(set(paths)):
            raise ValueError("conflicting relative paths")
        if self.preview_id is None and paths:
            raise ValueError("directory files require a confirmed preview")
        return self


class SkippedEntry(StrictRecord):
    relative_path: str
    reason: Literal["SYMLINK", "EXCLUDED_DIRECTORY", "SYSTEM_FILE", "TEMPORARY_FILE",
                    "UNSUPPORTED_EXTENSION", "NON_REGULAR"]

    @field_validator("relative_path")
    @classmethod
    def validate_relative(cls, value):
        return relative_path(value)


class ImageRelation(StrictRecord):
    markdown_relative_path: str
    image_reference: str
    resolved_relative_path: str | None
    status: Literal["RESOLVED", "MISSING", "AMBIGUOUS", "OUTSIDE_ROOT", "REMOTE"]
    syntax: Literal["MARKDOWN", "OBSIDIAN", "HTML"]
    reason: str
    candidate_relative_paths: list[str]

    @field_validator("markdown_relative_path", "resolved_relative_path")
    @classmethod
    def validate_relative(cls, value):
        return relative_path(value) if value is not None else None


class LocalAssetSnapshot(StrictRecord):
    markdown_relative_path: str
    relative_path: str
    size_bytes: int = Field(ge=0, le=FILE_LIMIT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("markdown_relative_path", "relative_path")
    @classmethod
    def validate_relative(cls, value):
        return relative_path(value)

    @model_validator(mode="after")
    def image_only(self):
        if PurePosixPath(self.relative_path).suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError("local asset must be a supported image")
        return self


class DirectoryPreviewRequest(StrictRecord):
    kb_id: UUID4
    absolute_root: str = Field(min_length=1, max_length=8192)
    source_root_id: UUID4 | None = None
    scope: Literal["DIRECTORY", "SELECTION"] = "DIRECTORY"
    files: list[FileItem]
    skipped: list[SkippedEntry]
    image_relations: list[ImageRelation]
    local_asset_snapshots: list[LocalAssetSnapshot] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def require_paths(self):
        paths = [item.relative_path for item in self.files]
        if any(value is None for value in paths) or len(paths) != len(set(paths)):
            raise ValueError("each preview file requires a unique relative path")
        asset_keys = [(item.markdown_relative_path, item.relative_path) for item in self.local_asset_snapshots]
        if len(asset_keys) != len(set(asset_keys)) or any(note not in paths for note, _path in asset_keys):
            raise ValueError("each local asset must bind a unique selected Markdown input")
        if self.local_asset_snapshots and self.scope != "SELECTION":
            raise ValueError("local asset snapshots require explicit selection scope")
        return self


class PreviewConfirmation(StrictRecord):
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]


class RootRebind(StrictRecord):
    absolute_root: str = Field(min_length=1, max_length=8192)
    binding_version: int = Field(ge=1)


class RenameAssociation(StrictRecord):
    document_id: UUID4
    source_root_id: UUID4
    old_relative_path: str
    new_relative_path: str

    @field_validator("old_relative_path", "new_relative_path")
    @classmethod
    def validate_relative(cls, value):
        return relative_path(value)


class URLRequest(StrictRecord):
    kb_id: UUID4
    url: str = Field(min_length=1, max_length=8192)
    document_id: UUID4 | None = None
    profile: Literal["normal", "bounded_scroll"] = "normal"
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return url_source_key(value)


class RecoveryRequest(StrictRecord):
    expected_attempt_id: UUID4
    confirm_stopped: Literal[True]


class ModeRequest(StrictRecord):
    mode: Literal["IMPORT", "QUERY"]
    kb_id: UUID4 | None = None


class KnowledgeBaseCreate(StrictRecord):
    name: str = Field(min_length=1, max_length=160)
