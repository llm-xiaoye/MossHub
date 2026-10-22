from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, TypedDict
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

MIB = 1024 * 1024
MAX_FILE = 50 * MIB
EXTENSIONS = {
    ".pdf", ".md", ".txt", ".html", ".htm", ".docx", ".xlsx", ".csv", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".svg",
}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".svg"}


class DirectoryPreviewRequest(TypedDict):
    kb_id: str
    absolute_root: str
    source_root_id: str | None
    files: list[dict[str, Any]]
    skipped: list[dict[str, str]]
    image_relations: list[dict[str, Any]]


def relative(value: str) -> str:
    if (
        not value or value.startswith("/") or "\\" in value or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"无效 POSIX 相对路径：{value!r}")
    return value


def _key(s: os.stat_result) -> tuple[int, ...]:
    return (
        s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size,
        s.st_mtime_ns, s.st_ctime_ns,
    )


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def root_fd(root: str) -> Iterator[int]:
    fd = os.open(root, _dir_flags())
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_file(fd: int, rel: str) -> Iterator[tuple[Any, os.stat_result, int, str]]:
    """逐级打开目录；不跟随任一相对路径分量的 symlink。"""
    parts = relative(rel).split("/")
    parent = os.dup(fd)
    file_fd = None
    stream = None
    try:
        for part in parts[:-1]:
            child = os.open(part, _dir_flags(), dir_fd=parent)
            os.close(parent)
            parent = child
        leaf = parts[-1]
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{rel}: 不是普通文件")
        if before.st_size > MAX_FILE:
            raise ValueError(f"{rel}: 超过单文件 50 MiB")
        file_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        if _key(os.fstat(file_fd)) != _key(before):
            raise ValueError(f"{rel}: 打开期间发生变化")
        stream = os.fdopen(file_fd, "rb")
        file_fd = None
        yield stream, before, parent, leaf
    finally:
        if stream is not None:
            stream.close()
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent)


def unchanged(
    stream: Any, before: os.stat_result, parent: int, leaf: str, rel: str
) -> None:
    current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    if _key(before) != _key(current) or _key(before) != _key(os.fstat(stream.fileno())):
        raise ValueError(f"{rel}: 读取期间发生变化")


def digest_stream(stream: Any, keep: bool = False) -> tuple[int, str, bytes | None]:
    digest = hashlib.sha256()
    count = 0
    chunks = [] if keep else None
    while chunk := stream.read(MIB):
        count += len(chunk)
        if count > MAX_FILE:
            raise ValueError("读取中的文件超过 50 MiB")
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return count, digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def _obsidian_rule(state: Any, silent: bool) -> bool:
    start = state.pos
    if not state.src.startswith("![[", start):
        return False
    end = state.src.find("]]", start + 3, state.posMax)
    if end < 0:
        return False
    raw = state.src[start + 3:end]
    if not raw.strip() or "\n" in raw or "\r" in raw:
        return False
    if not silent:
        token = state.push("obsidian_embed", "", 0)
        token.meta = {"reference": raw}
    state.pos = end + 2
    return True


def _references(content: bytes) -> list[tuple[str, str]]:
    from html.parser import HTMLParser
    result = []
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() == "img":
                result.extend(("HTML", value) for key, value in attrs
                              if key.lower() == "src" and value)
    html = Images()
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.before("image", "obsidian_embed", _obsidian_rule)
    text = content.decode("utf-8-sig")
    for block in parser.parse(text):
        if block.type == "html_block":
            html.feed(block.content)
        if block.type != "inline":
            continue
        for token in block.children or []:
            if token.type == "image":
                target = token.attrGet("src")
                if isinstance(target, str):
                    result.append(("MARKDOWN", target))
            elif token.type == "obsidian_embed":
                result.append(("OBSIDIAN", token.meta["reference"]))
            elif token.type == "html_inline":
                html.feed(token.content)
    html.close()
    return list(dict.fromkeys(result))

def _relation(
    note: str, syntax: str, reference: str, images: set[str]
) -> dict[str, Any] | None:
    record = {
        "markdown_relative_path": note,
        "image_reference": reference,
        "resolved_relative_path": None,
        "status": "MISSING",
        "syntax": syntax,
        "reason": "",
        "candidate_relative_paths": [],
    }
    target = reference.split("|", 1)[0].strip() if syntax == "OBSIDIAN" else reference
    parsed = urlsplit(target)
    if parsed.scheme == "file":
        record.update(status="OUTSIDE_ROOT", reason="FILE_URI_NOT_FOLLOWED")
        return record
    if parsed.scheme or parsed.netloc:
        record.update(status="REMOTE", reason="NON_LOCAL_REFERENCE_NOT_FETCHED")
        return record

    path = unquote(parsed.path, encoding="utf-8", errors="strict")
    if "\\" in path or "\x00" in path:
        raise ValueError(f"{note}: 不支持的图片路径 {reference!r}")
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return None
    if syntax == "OBSIDIAN" and suffix not in IMAGES:
        return None
    if not path or path.startswith("/"):
        record.update(status="OUTSIDE_ROOT", reason="EMPTY_OR_ABSOLUTE_REFERENCE")
        return record

    local = posixpath.normpath(posixpath.join(posixpath.dirname(note), path))
    if local == ".." or local.startswith("../"):
        record.update(status="OUTSIDE_ROOT", reason="REFERENCE_ESCAPES_ROOT")
        return record
    if syntax in {"MARKDOWN", "HTML"} or path.startswith(("./", "../")):
        candidates = [local] if local in images else []
    elif "/" not in path:
        candidates = sorted(p for p in images if PurePosixPath(p).name == path)
    else:
        candidates = sorted({p for p in (posixpath.normpath(path), local) if p in images})

    record["candidate_relative_paths"] = candidates
    if len(candidates) == 1:
        record.update(status="RESOLVED", resolved_relative_path=candidates[0],
                      reason="SELECTED_SCANNED_IMAGE")
    elif len(candidates) > 1:
        record.update(status="AMBIGUOUS", reason="MULTIPLE_SCANNED_IMAGES")
    else:
        record["reason"] = "NO_SELECTED_SCANNED_IMAGE"
    return record


def _provenance(path: str) -> dict[str, Any]:
    return {
        "nature": "ORIGINAL",
        "origin_url": None,
        "origin_local_path": path,
        "source_revision": None,
        "source_date": None,
        "license": None,
        "derivation": None,
    }


def _item(fd: int, root: str, rel: str) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix not in EXTENSIONS:
        raise ValueError(f"{rel}: 不支持的文件扩展名")
    with open_file(fd, rel) as (stream, before, parent, leaf):
        size, digest, content = digest_stream(stream, keep=suffix == ".md")
        unchanged(stream, before, parent, leaf, rel)
        if size != before.st_size:
            raise ValueError(f"{rel}: 字节数不一致")
    refs = _references(content) if content is not None else []
    return {
        "filename": PurePosixPath(rel).name,
        "relative_path": rel,
        "size_bytes": size,
        "sha256": digest,
        "document_id": None,
        "provenance": _provenance(str(Path(root) / rel)),
    }, refs


def describe_file(path: str) -> tuple[str, dict[str, Any]]:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise ValueError("不能上传符号链接")
    resolved = supplied.resolve(strict=True)
    root = str(resolved.parent)
    with root_fd(root) as fd:
        item, _ = _item(fd, root, resolved.name)
    return root, item


def scan(root: str, kb_id: str, root_id: str | None = None, *, max_files: int | None = None, max_bytes: int | None = None, max_entries: int | None = None) -> DirectoryPreviewRequest:
    parsed_kb = uuid.UUID(kb_id)
    if parsed_kb.version != 4:
        raise ValueError("kb_id 必须是 UUID4")
    kb_id = str(parsed_kb)
    if root_id is not None:
        parsed_root = uuid.UUID(root_id)
        if parsed_root.version != 4:
            raise ValueError("source_root_id 必须是 UUID4")
        root_id = str(parsed_root)
    supplied = Path(root).expanduser()
    if supplied.is_symlink():
        raise ValueError("所选根不能是符号链接")
    canonical = str(supplied.resolve(strict=True))
    files, skipped, pending = [], [], []
    seen_entries=0;total_bytes=0;deadline=time.monotonic()+30.0

    with root_fd(canonical) as fd:
        root_before = os.fstat(fd)

        def walk(current: int, prefix: str) -> None:
            nonlocal seen_entries,total_bytes
            before = os.fstat(current)
            with os.scandir(current) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                seen_entries+=1
                if max_entries is not None and seen_entries>max_entries:raise ValueError("目录对象超过预览上限，请缩小目录")
                if max_entries is not None and time.monotonic()>deadline:raise ValueError("目录预览超过30秒，请缩小目录")
                rel = relative(f"{prefix}/{name}" if prefix else name)
                observed = os.stat(name, dir_fd=current, follow_symlinks=False)
                reason = None
                if stat.S_ISLNK(observed.st_mode):
                    reason = "SYMLINK"
                elif stat.S_ISDIR(observed.st_mode):
                    if name in {".git", ".obsidian"}:
                        reason = "EXCLUDED_DIRECTORY"
                    else:
                        child = os.open(name, _dir_flags(), dir_fd=current)
                        try:
                            if _key(os.fstat(child)) != _key(observed):
                                raise ValueError(f"{rel}: 目录发生变化")
                            walk(child, rel)
                            now = os.stat(name, dir_fd=current, follow_symlinks=False)
                            if _key(now) != _key(os.fstat(child)):
                                raise ValueError(f"{rel}: 目录被替换")
                        finally:
                            os.close(child)
                        continue
                elif not stat.S_ISREG(observed.st_mode):
                    reason = "NON_REGULAR"
                elif name == ".DS_Store":
                    reason = "SYSTEM_FILE"
                elif (
                    name.startswith(("~$", ".~")) or name.endswith("~")
                    or name.lower().endswith((".tmp", ".temp", ".swp", ".swo"))
                ):
                    reason = "TEMPORARY_FILE"
                elif PurePosixPath(name).suffix.lower() not in EXTENSIONS:
                    reason = "UNSUPPORTED_EXTENSION"

                if reason:
                    skipped.append({"relative_path": rel, "reason": reason})
                    continue
                if max_files is not None and len(files)>=max_files:raise ValueError("目录文件超过50项，请分批导入")
                total_bytes+=observed.st_size
                if max_bytes is not None and total_bytes>max_bytes:raise ValueError("目录文件超过500 MiB，请分批导入")
                item, refs = _item(fd, canonical, rel)
                files.append(item)
                pending.extend((rel, syntax, reference) for syntax, reference in refs)

            if _key(before) != _key(os.fstat(current)):
                raise ValueError(f"{prefix or '.'}: 扫描时目录发生变化")

        walk(fd, "")
        now = os.stat(canonical, follow_symlinks=False)
        if _key(root_before) != _key(os.fstat(fd)) or _key(root_before) != _key(now):
            raise ValueError("扫描时根目录发生变化")

    files.sort(key=lambda item: item["relative_path"])
    skipped.sort(key=lambda item: (item["relative_path"], item["reason"]))
    if len({item["relative_path"] for item in files}) != len(files):
        raise ValueError("相对路径冲突")
    images = {
        item["relative_path"] for item in files
        if PurePosixPath(item["filename"]).suffix.lower() in IMAGES
    }
    relations = []
    for note, syntax, reference in pending:
        relation = _relation(note, syntax, reference, images)
        if relation is not None:
            relations.append(relation)
    relations.sort(key=lambda r: (
        r["markdown_relative_path"], r["syntax"], r["image_reference"],
        r["resolved_relative_path"] or "",
    ))
    return {
        "kb_id": kb_id,
        "absolute_root": canonical,
        "source_root_id": root_id,
        "files": files,
        "skipped": skipped,
        "image_relations": relations,
    }
