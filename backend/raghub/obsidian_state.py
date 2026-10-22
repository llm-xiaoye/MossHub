"""Bounded reads of registered Obsidian sources; never writes to the vault."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from contextlib import closing
from urllib.parse import quote

from .db import now, open_db
from .ingestion.store import BusinessError, require_instance, require_kb


class ObsidianState:
    def __init__(self, vault):
        self.vault = vault
        self._digests = OrderedDict()
        self._lock = threading.Lock()

    def _rows(self, db, kb_id, after="", limit=50, document_id=None):
        # A renamed source becomes current only when its archive is adopted.
        # Historical paths remain in SQLite, but are not change-reminder targets.
        query = """
            SELECT s.source_id,s.document_id,s.kb_id,s.relative_path,d.display_name,
                   d.current_raw_version_id,d.current_processing_version_id,
                   p.status AS processing_status,t.status AS task_status,t.stage,
                   gd.raw_version_id AS published_raw_version_id,
                   gd.processing_version_id AS published_processing_version_id,
                   (SELECT i.payload_json FROM source_observations o
                    JOIN task_inputs i ON i.task_id=o.task_id
                    WHERE o.source_id=s.source_id AND o.raw_version_id=d.current_raw_version_id
                    ORDER BY o.created_at DESC,o.observation_id DESC LIMIT 1) AS archived_payload_json,
                   pending.payload_json AS pending_payload_json
            FROM document_sources s
            JOIN source_roots r ON r.source_root_id=s.source_root_id
            JOIN documents d ON d.document_id=s.document_id AND d.kb_id=s.kb_id
            LEFT JOIN processing_versions p ON p.processing_version_id=d.current_processing_version_id
            LEFT JOIN tasks t ON t.task_id=d.active_prepare_task_id
            LEFT JOIN task_inputs pending ON pending.task_id=d.active_prepare_task_id
            LEFT JOIN generations g ON g.kb_id=d.kb_id AND g.state='CURRENT'
            LEFT JOIN generation_documents gd ON gd.generation_id=g.generation_id AND gd.document_id=d.document_id
            WHERE s.kind='DIRECTORY' AND r.absolute_path=? AND s.kb_id=?
              AND d.removed_at IS NULL AND s.source_id>?
              AND s.source_id=COALESCE(
                  (SELECT o.source_id FROM source_observations o
                   JOIN document_sources os ON os.source_id=o.source_id
                   WHERE os.document_id=d.document_id AND o.raw_version_id=d.current_raw_version_id
                   ORDER BY o.created_at DESC,o.observation_id DESC LIMIT 1),
                  pending.source_id,s.source_id)
        """
        args = [str(self.vault.root), kb_id, after]
        if document_id is not None:
            query += " AND s.document_id=?"
            args.append(document_id)
        query += " ORDER BY s.source_id LIMIT ?"
        args.append(limit)
        return [dict(row) for row in db.execute(query, tuple(args)).fetchall()]

    def _digest(self, fd, relative, *, fresh=False):
        relative = self.vault.path(relative)
        helper = self.vault.scanner()
        with helper.open_file(fd, relative) as (stream, before, parent, leaf):
            key = (relative, self.vault.signature(before))
            with self._lock:
                cached = None if fresh else self._digests.get(key)
                if cached is not None:
                    self._digests.move_to_end(key)
            if cached is None:
                size, digest, _ = helper.digest_stream(stream)
                cached = (size, digest)
            helper.unchanged(stream, before, parent, leaf, relative)
            with self._lock:
                self._digests[key] = cached
                self._digests.move_to_end(key)
                while len(self._digests) > 4096:
                    self._digests.popitem(last=False)
            return cached

    def _file(self, fd, relative, expected, *, fresh=False):
        try:
            size, digest = self._digest(fd, relative, fresh=fresh)
        except FileNotFoundError:
            return "MISSING"
        except (OSError, ValueError, BusinessError):
            return "UNREADABLE"
        return "UNCHANGED" if (size, digest) == expected else "CHANGED"

    def _inspect(self, row, fd, *, fresh=False):
        output = {key: row[key] for key in (
            "source_id", "document_id", "kb_id", "relative_path", "display_name",
            "current_raw_version_id", "current_processing_version_id",
            "processing_status", "task_status", "stage", "published_raw_version_id",
            "published_processing_version_id",
        )}
        output["latest_archive_published"] = bool(
            row["current_processing_version_id"]
            and row["current_processing_version_id"] == row["published_processing_version_id"]
        )
        raw = row["archived_payload_json"] or row["pending_payload_json"]
        if raw is None:
            return {**output, "state": "NOT_READY", "note_state": "NOT_READY", "changes": []}
        payload = json.loads(raw)
        metadata = payload["metadata"]
        if (payload["kind"] != "DIRECTORY" or payload["source_id"] != row["source_id"]
                or payload["document_id"] != row["document_id"] or payload["kb_id"] != row["kb_id"]
                or metadata["relative_path"] != row["relative_path"]):
            raise BusinessError("OBSIDIAN_SOURCE_BINDING_CHANGED",
                                "登记来源与归档记录不一致，已停止检查；资料与历史回答仍保留。", 409)
        note_state = self._file(fd, row["relative_path"], (metadata["size_bytes"], metadata["sha256"]), fresh=fresh)
        changes = []
        if note_state != "UNCHANGED":
            changes.append({"kind": "NOTE", "relative_path": row["relative_path"], "state": note_state})
        seen = set()
        for asset in payload.get("local_asset_uploads", []):
            relative = self.vault.path(asset["relative_path"])
            if relative in seen:
                continue
            seen.add(relative)
            state = self._file(fd, relative, (asset["size_bytes"], asset["sha256"]), fresh=fresh)
            if state != "UNCHANGED":
                changes.append({"kind": "IMAGE", "relative_path": relative, "state": state})
        state = ("MISSING" if any(item["state"] == "MISSING" for item in changes)
                 else "UNREADABLE" if any(item["state"] == "UNREADABLE" for item in changes)
                 else "CHANGED" if changes else "UNCHANGED")
        return {**output, "state": state, "note_state": note_state, "changes": changes}

    def changes(self, instance, kb_id, after="", limit=50):
        with closing(open_db(self.vault.settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            require_kb(db, kb_id)
            rows = self._rows(db, kb_id, after, limit + 1)
        page, more = rows[:limit], len(rows) > limit
        items = []
        if page:
            with self.vault.root_fd() as fd:
                items = [self._inspect(row, fd) for row in page]
        with closing(open_db(self.vault.settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            # A removal during file IO must not produce an actionable stale row.
            active = {row["document_id"] for row in db.execute(
                "SELECT document_id FROM documents WHERE kb_id=? AND removed_at IS NULL", (kb_id,)
            ).fetchall()}
            items = [item for item in items if item["document_id"] in active]
        return {"kb_id": kb_id, "items": items, "checked_at": now(),
                "next_cursor": page[-1]["source_id"] if more and page else None,
                "checked_count": len(page), "scope": "REGISTERED_SOURCES_ONLY",
                "local_missing_is_removal": False}

    def target(self, instance, document_id, raw_version_id=None, *, fresh=False):
        with closing(open_db(self.vault.settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            document = db.execute(
                "SELECT * FROM documents WHERE document_id=? AND removed_at IS NULL", (document_id,)
            ).fetchone()
            if document is None:
                raise BusinessError("SOURCE_NOT_ACCESSIBLE", "资料不存在或已移除，不能打开本地笔记。", 404)
            if raw_version_id is not None and db.execute(
                "SELECT raw_version_id FROM raw_versions WHERE document_id=? AND raw_version_id=?",
                (document_id, raw_version_id),
            ).fetchone() is None:
                raise BusinessError("SOURCE_NOT_ACCESSIBLE", "该归档版本不属于当前资料。", 404)
            rows = self._rows(db, document["kb_id"], document_id=document_id, limit=2)
        if not rows:
            return {"available": False, "document_id": document_id,
                    "reason": "该资料没有当前 Obsidian 来源。"}
        if len(rows) != 1:
            raise BusinessError("OBSIDIAN_SOURCE_AMBIGUOUS", "当前笔记来源不唯一，请先核对来源关联。", 409)
        with self.vault.root_fd() as fd:
            inspected = self._inspect(rows[0], fd, fresh=fresh)
        if inspected["note_state"] in {"MISSING", "UNREADABLE", "NOT_READY"}:
            return {**inspected, "available": False,
                    "reason": "本地笔记缺失、不可读或尚未归档；归档资料和历史回答仍保留。"}
        with closing(open_db(self.vault.settings.sqlite)) as db:
            require_instance(db, instance, importing=False)
            current = self._rows(db, inspected["kb_id"], document_id=document_id, limit=2)
            if not current:
                raise BusinessError("SOURCE_NOT_ACCESSIBLE", "资料已移除或来源关联已改变，不能打开旧入口。", 404)
            if (len(current) != 1 or current[0]["source_id"] != inspected["source_id"]
                    or current[0]["current_raw_version_id"] != inspected["current_raw_version_id"]):
                raise BusinessError("OBSIDIAN_SOURCE_BINDING_CHANGED", "来源在检查期间发生变化，请重新读取状态。", 409)
        path = self.vault.root / self.vault.path(inspected["relative_path"])
        different = (inspected["state"] != "UNCHANGED"
                     or (raw_version_id is not None and raw_version_id != inspected["current_raw_version_id"]))
        return {**inspected, "available": True, "uri": "obsidian://open?path=" + quote(str(path), safe=""),
                "comparison_raw_version_id": raw_version_id,
                "current_note_differs_from_archive": bool(different),
                "message": "将打开当前本地笔记；回答引用仍对应归档版本。" if different
                           else "将打开当前本地笔记；归档原文仍可独立查看。"}
