"""One CPU executor, bounded operations and complete-input model contracts."""
from collections import OrderedDict, deque
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4
import asyncio
import json
import math
import os
import threading
import time

import numpy as np
import psutil
from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .db import open_db, system_state
from .model_assets import verify_local_models
from .model_profiles import profiles
from .process_locks import service_role

REQUEST_MAX = 1024 * 1024
RESPONSE_MAX = 512 * 1024
RECORDS_MAX = 256
RETENTION = 600.0
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
ID_KEYS = ("embedding_space_id", "execution_profile_id", "reranker_profile_id")


def wire(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def uuid_string(value):
    if type(value) is not str:
        raise ValueError("canonical UUID4 string required")
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("canonical UUID4 string required")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Common(Strict):
    service_instance_id: str
    operation_id: str
    owner_id: str
    execution_profile_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    budget_ms: int = Field(ge=1, le=30000)

    @field_validator("service_instance_id", "operation_id", "owner_id")
    @classmethod
    def ids(cls, value):
        return uuid_string(value)


class EmbedItem(Strict):
    item_id: str = Field(min_length=1, max_length=128)
    text: str
    input_kind: Literal["query", "document_standard", "document_extended"]


class EmbedRequest(Common):
    embedding_space_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: list[EmbedItem] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("item IDs must be unique")
        return self


class RankItem(Strict):
    item_id: str = Field(min_length=1, max_length=128)
    text: str


class RankRequest(Common):
    reranker_profile_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    query: str
    items: list[RankItem] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("item IDs must be unique")
        return self


class CancelRequest(Strict):
    service_instance_id: str
    owner_id: str

    @field_validator("service_instance_id", "owner_id")
    @classmethod
    def ids(cls, value):
        return uuid_string(value)


class Reject(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class ModelFault(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Stopped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def error_value(error, code):
    return {"code": getattr(error, "code", code)[:64],
            "message": str(error)[:160], "error_type": type(error).__name__[:64]}


def check_stop(event, deadline):
    if event.is_set():
        raise Stopped("CANCELLED")
    if time.monotonic() >= deadline:
        raise Stopped("TIMED_OUT")


def token_ids(encoded):
    ids = encoded["input_ids"]
    if type(ids) is not list or any(type(value) is not int for value in ids):
        raise ModelFault("TOKENIZER_OUTPUT_INVALID", "One complete token sequence required")
    return ids


def embed_tokens(tokenizer, text):
    return len(token_ids(tokenizer(text, add_special_tokens=True, truncation=False, padding=False)))


def rank_tokens(tokenizer, query, passage):
    from FlagEmbedding.utils.tokenizer_compat import prepare_for_model_compat
    q = token_ids(tokenizer(query, add_special_tokens=False, truncation=False, padding=False))
    d = token_ids(tokenizer(passage, add_special_tokens=False, truncation=False, padding=False))
    if len(q) > 640 or len(d) > 1792:
        raise ModelFault("RERANK_SINGLE_INPUT_LIMIT", "Raw query or passage exceeds its limit")
    full = token_ids(prepare_for_model_compat(tokenizer, q, d, truncation=False,
                                             max_length=None, padding=False))
    if len(full) > 1792:
        raise ModelFault("RERANK_PAIR_TOKEN_LIMIT", "Complete actual pair exceeds 1792 tokens")
    bounded = token_ids(prepare_for_model_compat(tokenizer, q, d, truncation="only_second",
                                                max_length=1792, padding=False))
    if full != bounded:
        raise ModelFault("RERANK_TRUNCATION_DETECTED", "Production pair changes complete input IDs")
    return {"query_tokens_without_special": len(q), "document_tokens_without_special": len(d),
            "pair_tokens_with_special": len(full), "production_pair_exact": full == bounded}


def dense_values(value, count):
    array = np.asarray(value)
    if array.dtype != np.float32 or array.shape != (count, 1024) or not np.isfinite(array).all():
        raise ModelFault("DENSE_OUTPUT_INVALID", "Expected finite float32 (items,1024)")
    norms = np.linalg.norm(array.astype(np.float64), axis=1)
    if np.any(np.abs(norms - 1.0) > 1e-4):
        raise ModelFault("DENSE_NORMALIZATION_INVALID", "Output norm is outside tolerance")
    return array.tolist()


def score_values(value, count):
    array = np.asarray(value)
    if array.ndim == 0 and count == 1:
        array = array.reshape(1)
    if array.shape != (count,) or not np.issubdtype(array.dtype, np.number) or not np.isfinite(array).all():
        raise ModelFault("RERANK_OUTPUT_INVALID", "Expected one finite raw score per item")
    return [float(value) for value in array]


def cpu_parameters(module):
    import torch
    if not isinstance(module, torch.nn.Module):
        raise ModelFault("MODEL_PARAMETER_MODULE_INVALID", "Actual parameter carrier must be nn.Module")
    parameters = list(module.parameters())
    if not parameters or any(p.device.type != "cpu" or
                             (p.is_floating_point() and p.dtype != torch.float32) for p in parameters):
        raise ModelFault("MODEL_EXECUTION_PROFILE_INVALID", "Parameters must be CPU float32")
    module.eval()


class Engine:
    def __init__(self, embedding, reranker, revisions):
        self.embedding, self.reranker, self.revisions = embedding, reranker, revisions
        self.cache = OrderedDict()

    def encode(self, texts):
        result = self.embedding.encode(texts, batch_size=8, max_length=1024,
                                       return_dense=True, return_sparse=False, return_colbert_vecs=False)
        return dense_values(result["dense_vecs"], len(texts))

    def rank(self, pairs):
        result = self.reranker.compute_score(pairs, batch_size=4, query_max_length=640,
                                            max_length=1792, normalize=False)
        return score_values(result, len(pairs))

    def embed(self, request, stop, deadline):
        from .model_profiles import text_sha256, vector_key
        limits = {"query": 256, "document_standard": 512, "document_extended": 1024}
        counts, rank_counts, keys = {}, {}, []
        profile = {"embedding_space_id": request.embedding_space_id,
                   "execution_profile_id": request.execution_profile_id}
        for item in request.items:
            check_stop(stop, deadline)
            if item.text not in counts:
                counts[item.text] = embed_tokens(self.embedding.tokenizer, item.text)
            if counts[item.text] > limits[item.input_kind]:
                raise ModelFault("EMBED_TOKEN_LIMIT", "Complete input exceeds its token limit")
            if item.input_kind == "query":
                rank_counts[item.text] = embed_tokens(self.reranker.tokenizer, item.text)
                if rank_counts[item.text] > 256:
                    raise ModelFault("QUERY_RERANK_TOKEN_LIMIT", "Complete query exceeds reranker 256-token limit")
            keys.append(vector_key(profile, item.input_kind, item.text))
        resolved, missing, hits, invalidated = {}, OrderedDict(), {}, set()
        for item, key in zip(request.items, keys, strict=True):
            if key in resolved or key in missing:
                continue
            value = self.cache.get(key) if item.input_kind == "query" else None
            if value is not None:
                try:
                    if (type(value) is not tuple or len(value) != 1024
                            or any(type(v) not in {int, float} or not math.isfinite(v)
                                   or float(np.float32(v)) != v for v in value)):
                        raise ValueError("cached vector is not exact float32")
                    dense_values(np.asarray([value], dtype=np.float32), 1)
                except (ValueError, ModelFault, OverflowError):
                    self.cache.pop(key, None)
                    invalidated.add(key)
                    value = None
            hits[key] = value is not None
            if value is None:
                missing[key] = item.text
            else:
                resolved[key] = value
        check_stop(stop, deadline)
        if missing:
            vectors = self.encode(list(missing.values()))
            check_stop(stop, deadline)
            for key, value in zip(missing, vectors, strict=True):
                resolved[key] = tuple(value)
        result = {"items": [{"item_id": item.item_id, "dense": list(resolved[key])}
                            for item, key in zip(request.items, keys, strict=True)]}
        if all(item.input_kind == "query" for item in request.items):
            result["trace"] = {
                "query_inputs": [{"item_id": item.item_id, "input_sha256": text_sha256(item.text),
                                  "cache_key": key, "cache_hit": hits[key], "cache_invalidated": key in invalidated,
                                  "embedding_tokens": counts[item.text], "reranker_tokens": rank_counts[item.text]}
                                 for item, key in zip(request.items, keys, strict=True)],
                "model_compute_item_count": len(missing), "cache_capacity": 1024}
        actions = [(key, resolved[key]) for item, key in zip(request.items, keys, strict=True)
                   if item.input_kind == "query"]
        return result, actions

    def rerank(self, request, stop, deadline):
        pairs, observations = [], []
        for item in request.items:
            check_stop(stop, deadline)
            counts = rank_tokens(self.reranker.tokenizer, request.query, item.text)
            observations.append({"item_id": item.item_id,
                                 "document_tokens_without_special": counts["document_tokens_without_special"],
                                 "pair_tokens_with_special": counts["pair_tokens_with_special"],
                                 "production_pair_exact": counts["production_pair_exact"]})
            pairs.append([request.query, item.text])
        check_stop(stop, deadline)
        scores = self.rank(pairs)
        check_stop(stop, deadline)
        result = {"items": [{"item_id": item.item_id, "score": score}
                            for item, score in zip(request.items, scores, strict=True)],
                  "trace": {"query_tokens_without_special": counts["query_tokens_without_special"],
                            "pairs": observations, "score_type": "raw_logit", "batch_size": len(pairs)}}
        return result, []

    def cache_candidate(self, actions):
        if not actions:
            return None
        candidate = self.cache.copy()
        for key, value in actions:
            candidate[key] = value
            candidate.move_to_end(key)
            while len(candidate) > 1024:
                candidate.popitem(last=False)
        return candidate


def load_engine(settings):
    import torch
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    if torch.get_num_threads() != 4 or torch.get_num_interop_threads() != 1:
        raise ModelFault("TORCH_THREADS_INVALID", "Required threads are 4/1")
    revisions = verify_local_models(settings)
    from FlagEmbedding import BGEM3FlagModel, FlagReranker
    embedding = BGEM3FlagModel(str(settings.model_embedding), use_fp16=False, devices="cpu")
    reranker = FlagReranker(str(settings.model_reranker), use_fp16=False, devices="cpu")
    cpu_parameters(embedding.model)
    cpu_parameters(reranker.model)
    engine = Engine(embedding, reranker, revisions)
    embed_tokens(embedding.tokenizer, "warmup")
    rank_tokens(reranker.tokenizer, "warmup", "warmup")
    engine.encode(["warmup"])
    engine.rank([["warmup", "warmup"]])
    return engine


@dataclass
class Operation:
    header: dict
    fingerprint: bytes | None
    deadline: float | None
    request: Common | None = None
    state: str = "QUEUED"
    result: dict | None = None
    error: dict | None = None
    future: object = None
    timer: object = None
    stop: threading.Event = field(default_factory=threading.Event)
    retained_since: float | None = None
    submission_unconfirmed: bool = False

    def inflight(self):
        return self.submission_unconfirmed or (self.future is not None and not self.future.done())

    def pending(self):
        return self.submission_unconfirmed or self.future is not None


def success_packet(header, result):
    return {**header, "state": "SUCCEEDED", "compute_inflight": False,
            "completion_unconfirmed": False, "result": result, "error": None}


def run_job(engine, kind, request, header, stop, deadline):
    try:
        result, actions = getattr(engine, kind)(request, stop, deadline)
        if len(wire(success_packet(header, result))) > RESPONSE_MAX:
            raise ModelFault("RESPONSE_TOO_LARGE", "Complete response exceeds 512 KiB")
        check_stop(stop, deadline)
        return result, actions, None
    except BaseException as error:
        return None, [], error


class Runtime:
    def __init__(self, settings, ids, process, executor):
        self.settings, self.ids, self.process, self.executor = settings, ids, process, executor
        self.loop = asyncio.get_running_loop()
        self.instance = str(uuid4())
        self.operations, self.waiting = OrderedDict(), deque()
        self.active = None
        self.engine = None
        self.ready = self.stopping = self.load_unconfirmed = self.join_started = self.executor_stopped = False
        self.startup_error = self.executor_error = self.join_error = self.load_future = self.join_thread = None
        self.join_finished = asyncio.Event()

    def notify(self, callback, *args):
        try:
            self.loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass

    def start(self):
        try:
            self.load_future = self.executor.submit(load_engine, self.settings)
            self.load_future.add_done_callback(lambda future: self.notify(self.loaded, future))
        except Exception as error:
            self.load_unconfirmed = True
            self.startup_error = error_value(error, "MODEL_LOAD_SUBMISSION_FAILED")
            self.poison(error)

    def loaded(self, future):
        if self.load_future is not future or not future.done():
            return
        self.load_future = None
        try:
            self.engine = future.result()
            self.ready = not self.stopping
        except BaseException as error:
            self.startup_error = error_value(error, "MODEL_STARTUP_FAILED")
            self.ready = False

    def refresh(self):
        if self.load_future is not None and self.load_future.done():
            self.loaded(self.load_future)
        if self.active is not None:
            op = self.operations[self.active]
            if op.future is not None and op.future.done():
                self.completed(self.active, op.future)

    def header(self, operation, owner, kind, items, budget, tombstone=False):
        return {"service_instance_id": self.instance, "operation_id": operation, "owner_id": owner,
                **self.ids, "model_process": dict(self.process), "operation": kind,
                "ordered_item_ids": list(items), "budget_ms": budget, "tombstone": tombstone}

    def packet(self, op):
        return {**op.header, "state": op.state, "compute_inflight": op.inflight(),
                "completion_unconfirmed": op.submission_unconfirmed, "result": op.result, "error": op.error}

    def reap(self):
        stamp = time.monotonic()
        for key in list(self.operations):
            op = self.operations[key]
            if op.state in TERMINAL and not op.pending() and op.retained_since is not None and stamp-op.retained_since >= RETENTION:
                del self.operations[key]

    def terminal(self, op, state, error=None, result=None):
        op.state, op.error, op.result = state, error, result
        if op.timer is not None:
            op.timer.cancel()
            op.timer = None
        op.retained_since = None if op.pending() else time.monotonic()
        if not op.pending():
            op.request = None

    def check_instance(self, instance):
        if instance != self.instance:
            raise Reject(409, "SERVICE_INSTANCE_MISMATCH", "Operation belongs to another model instance")

    def admit(self, kind, request, received_at):
        self.check_instance(request.service_instance_id)
        if request.execution_profile_id != self.ids["execution_profile_id"]:
            raise Reject(409, "EXECUTION_PROFILE_MISMATCH", "Execution profile differs")
        key = "embedding_space_id" if kind == "embed" else "reranker_profile_id"
        if getattr(request, key) != self.ids[key]:
            raise Reject(409, "MODEL_PROFILE_MISMATCH", "Model profile differs")
        self.refresh()
        self.reap()
        fingerprint = wire({"operation": kind, "request": request.model_dump()})
        existing = self.operations.get(request.operation_id)
        if existing is not None:
            if existing.header["tombstone"]:
                raise Reject(409, "OPERATION_TOMBSTONED", "Cancelled before submission")
            if existing.fingerprint != fingerprint:
                raise Reject(409, "OPERATION_INPUT_MISMATCH", "Operation has a different complete input")
            return existing, False
        query_operation = kind == "rerank" or all(item.input_kind == "query" for item in request.items)
        if kind == "embed" and any(item.input_kind == "query" for item in request.items) and not query_operation:
            raise Reject(422, "INPUT_PURPOSE_MIXED", "One operation cannot mix query and document inputs")
        with closing(open_db(self.settings.sqlite)) as db:
            phase = system_state(db)["mode"]
        if phase not in ({"QUERY"} if query_operation else {"IMPORT", "DRAINING"}):
            raise Reject(409, "MODEL_PHASE_MISMATCH", "Import and query model work are separated")
        if not self.ready or self.stopping:
            raise Reject(503, "MODEL_NOT_READY", "Model service is not ready")
        if len(self.operations) >= RECORDS_MAX:
            raise Reject(429, "OPERATION_STORE_FULL", "Operation retention capacity is full")
        if self.active is not None and len(self.waiting) >= 1:
            raise Reject(429, "MODEL_QUEUE_FULL", "One active and one waiting operation allowed")
        op = Operation(self.header(request.operation_id, request.owner_id, kind,
                                   [item.item_id for item in request.items], request.budget_ms),
                       fingerprint, received_at+request.budget_ms/1000.0, request)
        if len(wire(self.packet(op))) > RESPONSE_MAX-2048:
            raise Reject(413, "RESPONSE_METADATA_TOO_LARGE", "Response metadata exceeds its budget")
        self.operations[request.operation_id] = op
        self.waiting.append(request.operation_id)
        op.timer = self.loop.call_later(max(0, op.deadline-time.monotonic()), self.timeout, request.operation_id)
        self.pump()
        self.refresh()
        return op, True

    def pump(self):
        if self.stopping or not self.ready or self.active is not None:
            return
        while self.waiting:
            key = self.waiting.popleft()
            op = self.operations[key]
            if op.state != "QUEUED":
                continue
            if time.monotonic() >= op.deadline:
                self.terminal(op, "TIMED_OUT", {"code": "BUDGET_EXHAUSTED"})
                continue
            self.active, op.state = key, "RUNNING"
            try:
                op.future = self.executor.submit(run_job, self.engine, op.header["operation"], op.request,
                                                 dict(op.header), op.stop, op.deadline)
                op.future.add_done_callback(lambda future, key=key: self.notify(self.completed, key, future))
            except Exception as error:
                op.submission_unconfirmed = True
                op.stop.set()
                self.terminal(op, "FAILED", error_value(error, "EXECUTOR_SUBMISSION_FAILED"))
                self.poison(error)
            return

    def timeout(self, key):
        op = self.operations.get(key)
        if op is None or op.state not in {"QUEUED", "RUNNING"}:
            return
        if op.future is not None and op.future.done():
            self.completed(key, op.future)
            return
        remaining = op.deadline-time.monotonic()
        if remaining > 0:
            op.timer = self.loop.call_later(remaining, self.timeout, key)
            return
        op.stop.set()
        if op.state == "QUEUED":
            self.waiting.remove(key)
            self.terminal(op, "TIMED_OUT", {"code": "BUDGET_EXHAUSTED"})
            self.pump()
        else:
            self.terminal(op, "TIMED_OUT", {"code": "BUDGET_EXHAUSTED", "cpu_completion": "PENDING"})

    def completed(self, key, future):
        op = self.operations.get(key)
        if op is None or op.future is not future or not future.done():
            return
        if self.active != key:
            raise RuntimeError("Active operation and actual Future differ")
        if op.submission_unconfirmed and not self.executor_stopped:
            return
        try:
            result, actions, error = future.result()
        except BaseException as caught:
            result, actions, error = None, [], caught
        op.future, self.active = None, None
        if op.submission_unconfirmed:
            op.submission_unconfirmed = False
            self.terminal(op, "FAILED", {**(op.error or {}), "executor_stop_confirmed": True})
        elif op.state in {"CANCELLING", "TIMED_OUT"}:
            state = "CANCELLED" if op.state == "CANCELLING" else "TIMED_OUT"
            self.terminal(op, state, {"code": state, "cpu_completion": "ENDED"})
        elif time.monotonic() >= op.deadline:
            self.terminal(op, "TIMED_OUT", {"code": "BUDGET_EXHAUSTED"})
        elif isinstance(error, Stopped):
            self.terminal(op, error.reason, {"code": error.reason})
        elif error is not None:
            self.terminal(op, "FAILED", error_value(error, "MODEL_EXECUTION_FAILED"))
        else:
            try:
                if len(wire(success_packet(op.header, result))) > RESPONSE_MAX:
                    raise ModelFault("RESPONSE_TOO_LARGE", "Complete response exceeds 512 KiB")
                cache = self.engine.cache_candidate(actions)
                if time.monotonic() >= op.deadline:
                    self.terminal(op, "TIMED_OUT", {"code": "BUDGET_EXHAUSTED"})
                else:
                    if cache is not None:
                        self.engine.cache = cache
                    self.terminal(op, "SUCCEEDED", result=result)
            except Exception as error:
                self.terminal(op, "FAILED", error_value(error, "RESULT_FINALIZATION_FAILED"))
        self.pump()

    def get(self, key, instance, owner):
        self.check_instance(instance)
        self.refresh()
        self.reap()
        op = self.operations.get(key)
        if op is None:
            raise Reject(404, "OPERATION_NOT_FOUND", "Unknown or outside retention; not a CPU-stop proof")
        if op.header["owner_id"] != owner:
            raise Reject(403, "OWNER_MISMATCH", "Operation owner differs")
        return op

    def cancel(self, key, request):
        self.check_instance(request.service_instance_id)
        self.refresh()
        self.reap()
        op = self.operations.get(key)
        if op is None:
            if self.stopping or len(self.operations) >= RECORDS_MAX:
                raise Reject(503, "CANCEL_NOT_RECORDED", "Cannot reserve a cancellation tombstone")
            op = Operation(self.header(key, request.owner_id, None, [], None, True), None, None)
            self.operations[key] = op
            self.terminal(op, "CANCELLED", {"code": "CANCEL_TOMBSTONE"})
            return op
        if op.header["owner_id"] != request.owner_id:
            raise Reject(403, "OWNER_MISMATCH", "Operation owner differs")
        if op.state == "QUEUED":
            self.waiting.remove(key)
            op.stop.set()
            self.terminal(op, "CANCELLED", {"code": "CANCELLED"})
            self.pump()
        elif op.state == "RUNNING":
            op.stop.set()
            op.state = "CANCELLING"
            if op.timer is not None:
                op.timer.cancel()
                op.timer = None
        return op

    def cancel_waiting(self):
        while self.waiting:
            op = self.operations[self.waiting.popleft()]
            op.stop.set()
            self.terminal(op, "CANCELLED", {"code": "SERVICE_STOPPING"})

    def poison(self, error):
        self.ready, self.stopping = False, True
        self.executor_error = error_value(error, "EXECUTOR_SUBMISSION_FAILED")
        self.cancel_waiting()
        self.ensure_join()

    def ensure_join(self):
        if self.join_started:
            return
        self.join_started = True
        self.join_thread = threading.Thread(target=self.join_executor, name="rag-model-join", daemon=False)
        try:
            self.join_thread.start()
        except Exception as error:
            self.join_error = error_value(error, "EXECUTOR_JOIN_THREAD_START_FAILED")
            self.join_executor()

    def join_executor(self):
        try:
            self.executor.shutdown(wait=True, cancel_futures=False)
        except BaseException as error:
            self.notify(self.join_failed, error)
        else:
            self.notify(self.confirm_stopped)

    def join_failed(self, error):
        self.ready, self.stopping = False, True
        self.join_error = error_value(error, "EXECUTOR_JOIN_FAILED")
        self.join_finished.set()

    def confirm_stopped(self):
        self.executor_stopped, self.ready, self.stopping = True, False, True
        self.refresh()
        self.load_unconfirmed = False
        if self.active is not None:
            op = self.operations[self.active]
            if op.submission_unconfirmed:
                op.submission_unconfirmed, self.active = False, None
                self.terminal(op, "FAILED", {**(op.error or {}), "executor_stop_confirmed": True})
            elif op.future is not None and not op.future.done():
                self.executor_stopped = False
                self.join_error = {"code": "EXECUTOR_COMPLETION_INCONSISTENT"}
        if self.engine is not None:
            self.engine.cache.clear()
        self.join_finished.set()

    def health(self):
        self.refresh()
        self.reap()
        op = self.operations[self.active] if self.active is not None else None
        loading = self.load_unconfirmed or (self.load_future is not None and not self.load_future.done())
        return {"live": True, "ready": self.ready and not self.stopping, "service_instance_id": self.instance,
                **self.ids, "model_process": dict(self.process), "compute_inflight": bool(loading or (op and op.inflight())),
                "completion_unconfirmed": bool(self.load_unconfirmed or (op and op.submission_unconfirmed)),
                "active_operation_id": self.active, "waiting_count": len(self.waiting),
                "startup_error": self.startup_error, "executor_error": self.executor_error, "join_error": self.join_error}

    async def stop_service(self):
        self.stopping, self.ready = True, False
        self.refresh()
        self.cancel_waiting()
        if self.active is not None:
            op = self.operations[self.active]
            op.stop.set()
            if op.state == "RUNNING":
                op.state = "CANCELLING"
                if op.timer is not None:
                    op.timer.cancel()
                    op.timer = None
        self.ensure_join()
        await self.join_finished.wait()
        if not self.executor_stopped:
            raise ModelFault("EXECUTOR_STOP_UNCONFIRMED", "Actual executor stop is not confirmed")


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def parse_body(request, schema):
    body = bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk) > REQUEST_MAX:
            raise Reject(413, "REQUEST_TOO_LARGE", "Request exceeds 1 MiB")
        body.extend(chunk)
    try:
        value = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=duplicate_keys,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        return schema.model_validate(value)
    except ValueError:
        raise Reject(422, "REQUEST_INVALID", "Invalid strict JSON request") from None


def response(value, status=200):
    data = wire(value)
    if len(data) > RESPONSE_MAX:
        raise Reject(500, "RESPONSE_LIMIT", "Response exceeds 512 KiB")
    return Response(data, status_code=status, media_type="application/json")


def runtime(request):
    value = getattr(request.app.state, "runtime", None)
    if value is None:
        raise Reject(503, "SERVICE_NOT_STARTED", "Service has not started")
    return value


def operation_key(value):
    try:
        return uuid_string(value)
    except ValueError:
        raise Reject(422, "OPERATION_ID_INVALID", "Invalid operation UUID") from None


@asynccontextmanager
async def lifespan(app):
    settings = Settings.load()
    with service_role(settings, "model"):
        selected = profiles(settings)
        ids = {key: selected[key] for key in ID_KEYS}
        process = {"pid": os.getpid(), "process_created_at": psutil.Process().create_time()}
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-model-cpu")
        state = None
        try:
            state = Runtime(settings, ids, process, executor)
            app.state.runtime = state
            state.start()
            yield
        finally:
            try:
                if state is not None:
                    await state.stop_service()
            finally:
                executor.shutdown(wait=True, cancel_futures=False)
                if state is not None:
                    state.confirm_stopped()
                    if state.join_thread is not None and state.join_thread.ident is not None:
                        state.join_thread.join()


app = FastAPI(title="RAG Hub local models", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def origin(request, call_next):
    supplied = request.headers.get("origin")
    if supplied is not None and supplied != Settings.load().frontend_origin:
        return response({"error": {"code": "ORIGIN_REJECTED"}}, 403)
    return await call_next(request)


@app.exception_handler(Reject)
async def rejected(request, error):
    state = getattr(request.app.state, "runtime", None)
    return response({"service_instance_id": state.instance if state else None,
                     "error": {"code": error.code, "message": str(error)[:160]}}, error.status)


@app.get("/health/live")
async def live(request: Request):
    return response(runtime(request).health())


@app.get("/health/ready")
async def ready(request: Request):
    state = runtime(request).health()
    return response(state, 200 if state["ready"] else 503)


@app.post("/embed")
async def embed(request: Request):
    received = time.monotonic()
    payload = await parse_body(request, EmbedRequest)
    state = runtime(request)
    op, created = state.admit("embed", payload, received)
    return response(state.packet(op), 202 if created else 200)


@app.post("/rerank")
async def rerank(request: Request):
    received = time.monotonic()
    payload = await parse_body(request, RankRequest)
    state = runtime(request)
    op, created = state.admit("rerank", payload, received)
    return response(state.packet(op), 202 if created else 200)


@app.get("/operations/{key}")
async def operation(key: str, request: Request):
    if set(request.query_params) != {"service_instance_id", "owner_id"}:
        raise Reject(422, "QUERY_INVALID", "Expected original instance and owner")
    for name in ("service_instance_id", "owner_id"):
        if len(request.query_params.getlist(name)) != 1:
            raise Reject(422, "QUERY_INVALID", "Duplicate identity parameter")
    try:
        instance, owner = uuid_string(request.query_params["service_instance_id"]), uuid_string(request.query_params["owner_id"])
    except ValueError:
        raise Reject(422, "QUERY_INVALID", "Invalid original identity") from None
    state = runtime(request)
    return response(state.packet(state.get(operation_key(key), instance, owner)))


@app.post("/operations/{key}/cancel")
async def cancel(key: str, request: Request):
    payload = await parse_body(request, CancelRequest)
    state = runtime(request)
    op = state.cancel(operation_key(key), payload)
    return response(state.packet(op), 202 if op.state == "CANCELLING" else 200)
