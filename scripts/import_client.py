from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from directory import MIB, digest_stream, open_file, root_fd, unchanged


ORIGIN = "http://127.0.0.1:5173"


class UnknownOutcome(RuntimeError):
    pass


class APIError(RuntimeError):
    pass


def emit(event: str, **fields: Any) -> None:
    print(json.dumps(
        {"event": event, **fields}, ensure_ascii=False, sort_keys=True,
        allow_nan=False,
    ), flush=True)


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise APIError(f"{label}: 缺少 UUID 字符串")
    parsed = uuid.UUID(value)
    if parsed.version != 4:
        raise APIError(f"{label}: 必须是 UUID4")
    return str(parsed)


class Client:
    def __init__(self, base: str = "http://127.0.0.1:8000"):
        self.http = httpx.Client(
            base_url=base.rstrip("/"),
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=5.0),
            headers={"Origin": ORIGIN},
        )
        try:
            state = self.get("/api/state")
            raw_instance = state.get("data_instance_id")
            _uuid(raw_instance, "data_instance_id")
            self.instance = raw_instance
            self.http.headers["X-RAG-Data-Instance"] = self.instance
        except BaseException:
            self.http.close()
            raise

    def close(self) -> None:
        self.http.close()

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(
        self, path: str, body: dict[str, Any] | None = None,
        *, files: Any = None, request_key: str | None = None,
    ) -> dict[str, Any]:
        key = _uuid(request_key or str(uuid.uuid4()), "request_key")
        return self._request(
            "POST", path, body=body, files=files, request_key=key,
        )

    def _request(
        self, method: str, path: str, *,
        body: dict[str, Any] | None = None, files: Any = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        writing = method == "POST"
        headers = {"X-Request-ID": request_key} if request_key else {}
        if writing:
            emit("SUBMITTING", path=path, request_key=request_key,
                 data_instance_id=self.instance)
        try:
            response = self.http.request(
                method, path, json=body, files=files, headers=headers,
            )
        except (httpx.HTTPError, KeyboardInterrupt) as exc:
            emit("REQUEST_INTERRUPTED", path=path, request_key=request_key,
                 error_type=type(exc).__name__, error=str(exc),
                 outcome="UNKNOWN" if writing else "READ_FAILED",
                 automatic_retry=False)
            if writing:
                raise UnknownOutcome("写响应丢失；查询既有记录，不重发") from exc
            raise APIError("读取失败") from exc

        if not 200 <= response.status_code < 300:
            emit("HTTP_ERROR", path=path, request_key=request_key,
                 status_code=response.status_code,
                 raw_body_hex=response.content.hex(),
                 business_outcome="NOT_INFERRED", automatic_retry=False)
            raise APIError(f"HTTP {response.status_code}，停止新提交")

        try:
            mime = response.headers.get("content-type", "").split(";", 1)[0]
            if mime.strip().lower() != "application/json":
                raise ValueError("响应不是 application/json")
            result = json.loads(response.content.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("响应根不是 object")
        except (UnicodeDecodeError, ValueError) as exc:
            emit("INVALID_RESPONSE", path=path, request_key=request_key,
                 raw_body_hex=response.content.hex(),
                 outcome="UNKNOWN" if writing else "READ_FAILED")
            if writing:
                raise UnknownOutcome("写响应不能解释，不重发") from exc
            raise APIError("读取响应不能解释") from exc
        return result


def batches(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result, group, size = [], [], 0
    for item in items:
        item_size = item["size_bytes"]
        if group and (len(group) == 50 or size + item_size > 500 * MIB):
            result.append(group)
            group, size = [], 0
        group.append(item)
        size += item_size
    if group:
        result.append(group)
    return result


def disk_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(item[key] for key in (
        "relative_path", "filename", "size_bytes", "sha256",
    ))


def upload(
    client: Client, root: str, batch_id: str,
    record: dict[str, Any], expected: dict[str, Any],
) -> dict[str, Any]:
    rel = expected["relative_path"] or expected["filename"]
    with root_fd(root) as fd:
        with open_file(fd, rel) as (stream, before, parent, leaf):
            size, digest, _ = digest_stream(stream)
            unchanged(stream, before, parent, leaf, rel)
            if size != expected["size_bytes"] or digest != expected["sha256"]:
                raise ValueError(f"{rel}: 确认后已变化，未发送")
            stream.seek(0)
            result = client.post(
                f"/api/import/batches/{batch_id}/items/{record['item_id']}/file",
                files={"file": (
                    expected["filename"], stream, "application/octet-stream",
                )},
            )
            try:
                task_id = _uuid(result.get("task_id"), "task_id")
                attempt_id = _uuid(result.get("attempt_id"), "attempt_id")
            except (APIError, ValueError) as exc:
                emit("INVALID_UPLOAD_ENVELOPE", response=result,
                     batch_id=batch_id, item_id=record["item_id"], outcome="UNKNOWN")
                raise UnknownOutcome("上传受理字段缺失，不重发") from exc

            emit("ITEM_ACCEPTED", relative_path=expected["relative_path"],
                 disk_relative_path=rel, batch_id=batch_id,
                 item_id=record["item_id"], task_id=task_id, attempt_id=attempt_id,
                 task_completion="NOT_QUERIED")
            unchanged(stream, before, parent, leaf, rel)
            return result


def submit_batch(
    client: Client, root: str, body: dict[str, Any],
) -> list[dict[str, Any]]:
    request_key = str(uuid.uuid4())
    created = client.post("/api/import/batches", body, request_key=request_key)
    try:
        batch_id = _uuid(created.get("batch_id"), "batch_id")
    except (APIError, ValueError) as exc:
        emit("INVALID_BATCH_ENVELOPE", request_key=request_key,
             response=created, outcome="UNKNOWN")
        raise UnknownOutcome("按 request key 查询，不能再次建批") from exc
    emit("BATCH_CREATED", batch_id=batch_id, request_key=request_key)

    observed = client.get(f"/api/import/batches/{batch_id}")
    records = observed.get("items")
    if not isinstance(records, list) or len(records) != len(body["items"]):
        raise APIError("批次成员数量不符；保留 batch_id 并查询")
    index = {}
    seen_ids = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("metadata"), dict):
            raise APIError("批次 item 形状不符")
        rel = record["metadata"].get("relative_path")
        item_id = _uuid(record.get("item_id"), "item_id")
        if rel in index or item_id in seen_ids:
            raise APIError("批次 item 重复")
        if record.get("state") != "AWAITING_UPLOAD":
            emit("EXISTING_ITEM", batch_id=batch_id, item=record)
            raise APIError("item 不是 AWAITING_UPLOAD，停止新发送")
        record["item_id"] = item_id
        index[rel] = record
        seen_ids.add(item_id)

    expected = {item["relative_path"]: item for item in body["items"]}
    if set(index) != set(expected):
        raise APIError("服务端批次成员与提交成员不符")
    for rel in expected:
        if disk_signature(index[rel]["metadata"]) != disk_signature(expected[rel]):
            raise APIError("服务端批次文件绑定不符")
    return [
        upload(client, root, batch_id, index[item["relative_path"]], item)
        for item in body["items"]
    ]
