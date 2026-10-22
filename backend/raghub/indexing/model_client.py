"""Bounded local model calls; uncertain requests are cancelled once, never replayed."""
from __future__ import annotations
import json
import math
import time

import httpx

from ..ingestion.store import BusinessError
from ..model_profiles import canonical, profiles
from ..worker_runtime import process_liveness
from .vector_cache import vector_values

PACKET_KEYS = {"service_instance_id", "operation_id", "owner_id", "embedding_space_id",
               "execution_profile_id", "reranker_profile_id", "model_process", "operation",
               "ordered_item_ids", "budget_ms", "tombstone", "state", "compute_inflight",
               "completion_unconfirmed", "result", "error"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
MAX_RESPONSE = 512 * 1024


class ModelCallError(BusinessError):
    def __init__(self, code, message, details):
        super().__init__(code, message)
        self.details = details


def object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate model JSON key")
        value[key] = item
    return value


def decode(raw):
    def reject(value):
        raise ValueError("nonfinite model JSON")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=reject)
    if type(value) is not dict:
        raise ValueError("model response must be an object")
    return value


def validate_process(value):
    if (type(value) is not dict or set(value) != {"pid", "process_created_at"}
            or type(value["pid"]) is not int or value["pid"] <= 0
            or type(value["process_created_at"]) not in {int, float}
            or not math.isfinite(value["process_created_at"])):
        raise ValueError("model process identity is invalid")
    return value


class ModelClient:
    def __init__(self, settings, profile=None, record=None):
        self.settings = settings
        self.profile = profile or profiles(settings)
        self.record = record or (lambda kind, value: None)
        self.base = f"http://{settings.model_host}:{settings.model_port}"
        self.http = httpx.Client(trust_env=False, follow_redirects=False)
        self.health = self.ready()

    def close(self):
        self.http.close()

    def request(self, method, path, *, timeout, payload=None, params=None):
        if timeout <= 0:
            raise TimeoutError("local model budget exhausted")
        limits = httpx.Timeout(timeout, connect=min(2, timeout), pool=min(2, timeout), write=min(5, timeout))
        with self.http.stream(method, self.base+path, content=canonical(payload) if payload is not None else None,
                              headers={"Content-Type": "application/json"}, params=params, timeout=limits) as response:
            data = bytearray()
            for block in response.iter_bytes():
                if len(data)+len(block) > MAX_RESPONSE:
                    raise ValueError("model response exceeds 512 KiB")
                data.extend(block)
            value = decode(bytes(data))
            return response.status_code, value

    def ready(self):
        status, value = self.request("GET", "/health/ready", timeout=2)
        if status != 200 or value.get("ready") is not True:
            raise ModelCallError("MODEL_NOT_READY", "本地模型服务尚未就绪。", {"health": value})
        validate_process(value["model_process"])
        for key in ("embedding_space_id", "execution_profile_id", "reranker_profile_id"):
            if value[key] != self.profile[key]:
                raise ModelCallError("MODEL_CONFIGURATION_CHANGED", "模型运行身份与建库快照不一致。", {"key": key})
        if type(value["service_instance_id"]) is not str:
            raise ValueError("model service instance is missing")
        return value

    def packet(self, value, registration, *, tombstone_allowed=False):
        if type(value) is not dict or set(value) != PACKET_KEYS:
            raise ValueError("model operation envelope keys differ")
        for key in ("service_instance_id", "operation_id", "owner_id", "model_process"):
            if value[key] != registration[key]:
                raise ValueError("model operation identity differs")
        for key in ("embedding_space_id", "execution_profile_id", "reranker_profile_id"):
            if value[key] != self.profile[key]:
                raise ValueError("model operation profile differs")
        validate_process(value["model_process"])
        if type(value["compute_inflight"]) is not bool or type(value["completion_unconfirmed"]) is not bool:
            raise ValueError("model execution flags are invalid")
        if type(value["tombstone"]) is not bool:
            raise ValueError("model tombstone flag is invalid")
        if value["tombstone"]:
            if (not tombstone_allowed or value["state"] != "CANCELLED" or value["operation"] is not None
                    or value["ordered_item_ids"] != [] or value["budget_ms"] is not None
                    or value["compute_inflight"] or value["completion_unconfirmed"] or value["result"] is not None):
                raise ValueError("invalid model cancellation tombstone")
        elif (value["operation"] != registration["operation"]
              or value["ordered_item_ids"] != registration["ordered_item_ids"]
              or value["budget_ms"] != registration["budget_ms"]):
            raise ValueError("model operation input order or budget differs")
        if value["state"] not in TERMINAL | {"QUEUED", "RUNNING", "CANCELLING"}:
            raise ValueError("unknown model operation state")
        if value["state"] == "SUCCEEDED":
            if value["compute_inflight"] or value["completion_unconfirmed"] or value["error"] is not None:
                raise ValueError("successful model operation has pending execution")
        elif value["result"] is not None:
            raise ValueError("unadoptable operation contains a result")
        return value

    def call(self, operation, items, *, owner_id, operation_id, fence, query=None):
        started = time.monotonic()
        deadline = started+30.0
        fence()
        payload = {"service_instance_id": self.health["service_instance_id"], "owner_id": owner_id,
                   "operation_id": operation_id, "execution_profile_id": self.profile["execution_profile_id"],
                   "budget_ms": max(1, min(30000, int((deadline-time.monotonic())*1000))), "items": items}
        if operation == "embed":
            payload["embedding_space_id"] = self.profile["embedding_space_id"]
        else:
            payload.update(reranker_profile_id=self.profile["reranker_profile_id"], query=query)
        registration = {key: payload[key] for key in ("service_instance_id", "operation_id", "owner_id", "budget_ms")}
        registration.update(model_process=self.health["model_process"], operation=operation,
                            ordered_item_ids=[item["item_id"] for item in items])
        self.record("MODEL_OPERATION_REGISTERED", registration)
        sent = False
        packet = None
        try:
            fence()
            if len(canonical(payload)) > 1024*1024:
                raise ValueError("model request exceeds 1 MiB")
            sent = True
            status, value = self.request("POST", "/"+operation, timeout=deadline-time.monotonic(), payload=payload)
            if status not in {200, 202}:
                raise ModelCallError("MODEL_REQUEST_REJECTED", "本地模型拒绝本次操作。", {"status": status, "response": value})
            packet = self.packet(value, registration)
            while packet["state"] not in TERMINAL:
                fence()
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("local model total budget exhausted")
                time.sleep(min(0.1, remaining))
                status, value = self.request("GET", "/operations/"+operation_id, timeout=deadline-time.monotonic(),
                                             params={"service_instance_id": registration["service_instance_id"], "owner_id": owner_id})
                if status != 200:
                    raise ModelCallError("MODEL_RESULT_UNCONFIRMED", "本地模型结果无法确认，不重发操作。", {"status": status, "response": value})
                packet = self.packet(value, registration)
            self.record("MODEL_OPERATION_OBSERVED", packet)
            if time.monotonic() >= deadline or packet["state"] != "SUCCEEDED":
                raise ModelCallError("MODEL_OPERATION_"+packet["state"], "本地模型没有在本次预算内成功完成。", {"operation": packet})
            fence()
            result = packet["result"]
            query_result = operation == "rerank" or all(item["input_kind"] == "query" for item in items)
            result_keys = {"items", "trace"} if query_result else {"items"}
            if type(result) is not dict or set(result) != result_keys or type(result["items"]) is not list:
                raise ValueError("model result shape differs")
            if query_result:
                from ..retrieval.model_client import AsyncModelClient
                AsyncModelClient.validate_result(self, operation, items, result)
            expected = registration["ordered_item_ids"]
            if len(result["items"]) != len(expected):
                raise ValueError("model result cardinality differs")
            values = []
            for item_id, item in zip(expected, result["items"], strict=True):
                field = "dense" if operation == "embed" else "score"
                if type(item) is not dict or set(item) != {"item_id", field} or item["item_id"] != item_id:
                    raise ValueError("model result item identity differs")
                if operation == "embed":
                    vector_values(item[field])
                    values.append({"item_id": item_id, "vector": item[field]})
                else:
                    if type(item[field]) not in {float, int} or not math.isfinite(item[field]):
                        raise ValueError("reranker score is not finite")
                    values.append(item)
            return {"operation": packet, "items": values}
        except Exception as error:
            details = {"registration": registration, "last_observed_operation": packet,
                       "application_post_attempts": int(sent), "physical_send_count": "NOT_OBSERVED",
                       "elapsed_seconds": time.monotonic()-started, "error_type": type(error).__name__}
            if sent and (packet is None or packet["state"] not in TERMINAL or packet["compute_inflight"]):
                try:
                    status, cancelled = self.request("POST", "/operations/"+operation_id+"/cancel", timeout=2,
                                                     payload={"service_instance_id": registration["service_instance_id"], "owner_id": owner_id})
                    if status not in {200, 202}:
                        raise ValueError("cancellation was not recorded")
                    details["cancel_observation"] = self.packet(cancelled, registration, tombstone_allowed=True)
                    self.record("MODEL_OPERATION_OBSERVED", details["cancel_observation"])
                except Exception as cancel_error:
                    details["cancel_error"] = {"type": type(cancel_error).__name__, "message": str(cancel_error)}
            self.record("MODEL_OPERATION_UNADOPTABLE", details)
            raise ModelCallError(getattr(error, "code", "MODEL_CALL_UNCONFIRMED"), str(error), details) from error

    def embed(self, items, *, owner_id, operation_id, fence):
        return self.call("embed", items, owner_id=owner_id, operation_id=operation_id, fence=fence)

    def rerank(self, query, items, *, owner_id, operation_id, fence):
        return self.call("rerank", items, owner_id=owner_id, operation_id=operation_id, fence=fence, query=query)


def require_task_operations_stopped(events, settings):
    """Check the captured operation history outside the recovery transaction."""
    registrations, last = {}, {}
    for event in events:
        details = json.loads(event["details_json"])
        if event["event_type"] == "MODEL_OPERATION_REGISTERED":
            key = details["operation_id"]
            if key in registrations and registrations[key] != details:
                raise BusinessError("OLD_MODEL_OPERATION_IDENTITY_CHANGED", "旧模型操作编号存在不一致的关联，不能恢复。")
            registrations[key] = details
        else:
            last[details["operation_id"]] = details
    if set(last) - set(registrations):
        raise BusinessError("OLD_MODEL_OPERATION_IDENTITY_CHANGED", "旧模型观察缺少注册关联，不能恢复。")
    with httpx.Client(trust_env=False, timeout=2) as client:
        for key, registration in registrations.items():
            process = validate_process(registration["model_process"])
            observed = last.get(key)
            if (observed and all(observed.get(field) == registration[field]
                                 for field in ("service_instance_id", "owner_id", "operation_id", "model_process"))
                    and observed.get("state") in TERMINAL and observed.get("compute_inflight") is False
                    and observed.get("completion_unconfirmed") is False):
                continue
            liveness = process_liveness(process["pid"], process["process_created_at"])
            if liveness == "STOPPED":
                continue
            if liveness != "LIVE":
                raise BusinessError("OLD_MODEL_OWNER_UNKNOWN", "旧模型进程是否停止尚未确认。")
            response = client.get(f"http://{settings.model_host}:{settings.model_port}/operations/{key}",
                                  params={"service_instance_id": registration["service_instance_id"], "owner_id": registration["owner_id"]})
            value = decode(response.content)
            if (response.status_code != 200 or value.get("service_instance_id") != registration["service_instance_id"]
                    or value.get("owner_id") != registration["owner_id"] or value.get("operation_id") != key
                    or value.get("model_process") != process or value.get("state") not in TERMINAL
                    or value.get("compute_inflight") is not False or value.get("completion_unconfirmed") is not False):
                raise BusinessError("OLD_MODEL_OPERATION_ACTIVE_OR_UNKNOWN", "旧模型计算仍活动或状态未知，不能恢复任务。")
