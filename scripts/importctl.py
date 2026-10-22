from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

from directory import describe_file, relative, scan
from import_client import (
    APIError, Client, UnknownOutcome, batches, disk_signature, emit, submit_batch,
)


def ident(value: str) -> str:
    parsed = uuid.UUID(value)
    if parsed.version != 4:
        raise ValueError("项目 ID 必须是 UUID4")
    return str(parsed)


def provenance(args: argparse.Namespace, local: str | None = None) -> dict[str, Any]:
    if args.nature != "ORIGINAL" and not (args.derivation or "").strip():
        raise ValueError("DERIVED/CONSTRUCTED 必须填写 --derivation")
    return {
        "nature": args.nature,
        "origin_url": getattr(args, "url", None),
        "origin_local_path": local,
        "source_revision": args.source_revision,
        "source_date": args.source_date,
        "license": args.license,
        "derivation": args.derivation,
    }


def preview_directory(client: Client, args: argparse.Namespace) -> None:
    manifest = scan(args.root, ident(args.kb), args.source_root)
    result = client.post("/api/import/directory/preview", manifest)
    emit("PREVIEW_NOT_CONFIRMED", preview=result, manifest=manifest)


def confirm_directory(client: Client, args: argparse.Namespace) -> None:
    if not args.confirmed:
        raise ValueError("必须明确提供 --confirmed")
    preview_id = ident(args.preview)
    path = f"/api/import/directory/previews/{preview_id}"
    preview = client.get(path)
    if preview.get("preview_id") != preview_id:
        raise APIError("preview_id 不符")
    if preview.get("preview_sha256") != args.digest:
        raise ValueError("digest 与服务端 preview 不符")
    if preview.get("status") not in {"PREVIEWED", "CONFIRMED", "CONSUMED"}:
        raise APIError("未知 preview 状态")
    if not isinstance(preview.get("batches"), list):
        raise APIError("preview.batches 不是 array")
    if preview["batches"]:
        emit("EXISTING_BATCHES_NO_NEW_SUBMISSION", preview=preview)
        return
    if preview["status"] == "CONSUMED":
        raise APIError("CONSUMED 缺少既有批次记录")

    manifest = preview.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise APIError("preview manifest 形状不符")
    current = scan(
        manifest["absolute_root"], manifest["kb_id"], preview["source_root_id"],
    )
    if current["absolute_root"] != manifest["absolute_root"]:
        raise ValueError("根路径发生变化，需显式 rebind")
    expected = manifest["files"]
    expected_signatures = sorted(disk_signature(item) for item in expected)
    current_signatures = sorted(disk_signature(item) for item in current["files"])
    if (
        expected_signatures != current_signatures
        or current["skipped"] != manifest.get("skipped")
        or current["image_relations"] != manifest.get("image_relations")
    ):
        raise ValueError("目录现场与 preview 不符；保留旧 preview，重新预览")
    if not expected:
        raise ValueError("没有可上传文件")

    if preview["status"] == "PREVIEWED":
        client.post(path + "/confirm", {
            "preview_sha256": args.digest, "confirmed": True,
        })
    confirmed = client.get(path)
    if (
        confirmed.get("preview_sha256") != args.digest
        or confirmed.get("status") != "CONFIRMED"
        or confirmed.get("batches") != []
    ):
        emit("CONFIRM_STATE_CHANGED", preview=confirmed)
        raise APIError("确认现场发生变化，停止新批次")

    accepted = 0
    for group in batches(expected):
        results = submit_batch(client, current["absolute_root"], {
            "kb_id": manifest["kb_id"],
            "items": group,
            "preview_id": preview_id,
            "preview_sha256": args.digest,
        })
        accepted += len(results)
    emit("UPLOADS_ACCEPTED_TASKS_PENDING", accepted_count=accepted,
         task_completion="NOT_QUERIED", parsing_completion="NOT_ESTABLISHED")


def run(client: Client, args: argparse.Namespace) -> None:
    command = args.command
    if command == "preview-directory":
        preview_directory(client, args)
    elif command == "confirm-directory":
        confirm_directory(client, args)
    elif command == "upload-file":
        root, item = describe_file(args.file)
        item["relative_path"] = None
        item["document_id"] = ident(args.document) if args.document else None
        item["provenance"] = provenance(
            args, item["provenance"]["origin_local_path"],
        )
        submit_batch(client, root, {
            "kb_id": ident(args.kb), "items": [item],
            "preview_id": None, "preview_sha256": None,
        })
    elif command == "url":
        result = client.post("/api/import/urls", {
            "kb_id": ident(args.kb),
            "url": args.url,
            "document_id": ident(args.document) if args.document else None,
            "profile": args.profile,
            "provenance": provenance(args),
        })
        emit("URL_REQUEST_RESPONSE", response=result, task_completion="NOT_QUERIED")
    elif command == "create-kb":
        emit("CREATE_KB_RESPONSE", response=client.post(
            "/api/knowledge-bases", {"name": args.name},
        ))
    elif command == "task":
        emit("TASK_OBSERVED", response=client.get(f"/api/tasks/{ident(args.id)}"))
    elif command == "batch-by-request":
        request_id = ident(args.id)
        emit("BATCH_OBSERVED", response=client.get(
            f"/api/import/batches/by-request/{request_id}",
        ))
    elif command == "cancel":
        emit("CANCEL_RESPONSE", response=client.post(
            f"/api/tasks/{ident(args.id)}/cancel",
        ))
    elif command == "recover":
        if not args.confirm_stopped:
            raise ValueError("必须明确提供 --confirm-stopped")
        emit("RECOVER_RESPONSE", response=client.post(
            f"/api/tasks/{ident(args.id)}/recover",
            {"expected_attempt_id": ident(args.expected_attempt),
             "confirm_stopped": True},
        ))
    elif command == "mode":
        emit("MODE_RESPONSE", response=client.post(
            "/api/mode", {"mode": args.mode,
                          "kb_id": ident(args.kb) if args.kb else None},
        ))
    elif command == "rebind-root":
        if not args.confirmed:
            raise ValueError("必须明确提供 --confirmed")
        selected = Path(args.root).expanduser()
        if selected.is_symlink():
            raise ValueError("新根不能是 symlink")
        canonical = selected.resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("新根不是目录")
        emit("REBIND_RESPONSE", response=client.post(
            f"/api/import/source-roots/{ident(args.id)}/rebind",
            {"absolute_root": str(canonical),
             "binding_version": args.binding_version},
        ))
    elif command == "associate-rename":
        if not args.confirmed:
            raise ValueError("必须明确提供 --confirmed")
        emit("ASSOCIATION_RESPONSE", response=client.post(
            "/api/import/directory/associate-rename",
            {"document_id": ident(args.document),
             "source_root_id": ident(args.source_root),
             "old_relative_path": relative(args.old_relative),
             "new_relative_path": relative(args.new_relative)},
        ))
    else:
        raise ValueError("未知命令")


def provenance_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nature", choices=("ORIGINAL", "DERIVED", "CONSTRUCTED"),
                        default="ORIGINAL")
    parser.add_argument("--source-revision")
    parser.add_argument("--source-date")
    parser.add_argument("--license")
    parser.add_argument("--derivation")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG 真实导入和管理入口")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview-directory")
    preview.add_argument("root")
    preview.add_argument("--kb", required=True)
    preview.add_argument("--source-root")
    confirm = commands.add_parser("confirm-directory")
    confirm.add_argument("preview")
    confirm.add_argument("--digest", required=True)
    confirm.add_argument("--confirmed", action="store_true")
    file_parser = commands.add_parser("upload-file")
    file_parser.add_argument("file")
    file_parser.add_argument("--kb", required=True)
    file_parser.add_argument("--document")
    provenance_options(file_parser)
    url_parser = commands.add_parser("url")
    url_parser.add_argument("url")
    url_parser.add_argument("--kb", required=True)
    url_parser.add_argument("--document")
    url_parser.add_argument("--profile", choices=("normal", "bounded_scroll"),
                            default="normal")
    provenance_options(url_parser)
    create = commands.add_parser("create-kb")
    create.add_argument("name")
    for name in ("task", "batch-by-request", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("id")
    recover = commands.add_parser("recover")
    recover.add_argument("id")
    recover.add_argument("--expected-attempt", required=True)
    recover.add_argument("--confirm-stopped", action="store_true")
    mode = commands.add_parser("mode")
    mode.add_argument("mode", choices=("IMPORT", "QUERY"))
    mode.add_argument("--kb")
    rebind = commands.add_parser("rebind-root")
    rebind.add_argument("id")
    rebind.add_argument("root")
    rebind.add_argument("--binding-version", type=int, required=True)
    rebind.add_argument("--confirmed", action="store_true")
    rename = commands.add_parser("associate-rename")
    rename.add_argument("document")
    rename.add_argument("--source-root", required=True)
    rename.add_argument("--old-relative", required=True)
    rename.add_argument("--new-relative", required=True)
    rename.add_argument("--confirmed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    client = None
    try:
        client = Client(args.api)
        run(client, args)
        return 0
    except UnknownOutcome as exc:
        emit("STOPPED_UNKNOWN", error=str(exc), automatic_retry=False)
        return 3
    except (APIError, ValueError, OSError, UnicodeDecodeError) as exc:
        emit("STOPPED", error_type=type(exc).__name__, error=str(exc),
             automatic_retry=False)
        return 2
    except KeyboardInterrupt:
        emit("INTERRUPTED", automatic_retry=False)
        return 130
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
