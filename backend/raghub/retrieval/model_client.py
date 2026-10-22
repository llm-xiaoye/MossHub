"""Async query operations share the existing model ownership and no-replay contract."""
from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4

import httpx

from ..indexing.model_client import (MAX_RESPONSE, TERMINAL, ModelCallError, ModelClient,
                                    decode, validate_process)
from ..indexing.vector_cache import vector_values
from ..model_profiles import canonical, text_sha256, vector_key
from ..worker_runtime import process_liveness


class QueryTokenGate:
    def __init__(self, settings):
        from transformers import AutoTokenizer
        self.embedding = AutoTokenizer.from_pretrained(str(settings.model_embedding), local_files_only=True)
        self.reranker = AutoTokenizer.from_pretrained(str(settings.model_reranker), local_files_only=True)

    def validate(self, inputs):
        values = []
        for query in inputs["unique_queries"]:
            counts = {}
            for name, tokenizer in (("embedding", self.embedding), ("reranker", self.reranker)):
                ids = tokenizer(query, add_special_tokens=True, truncation=False, padding=False)["input_ids"]
                if type(ids) is not list or any(type(v) is not int for v in ids) or not 1 <= len(ids) <= 256:
                    raise ValueError("完整查询在两种实际tokenizer中都须不超过256 tokens")
                counts[name + "_tokens"] = len(ids)
            values.append({"input_sha256": text_sha256(query), **counts})
        ids = self.reranker(inputs["rerank_query"], add_special_tokens=False,
                            truncation=False, padding=False)["input_ids"]
        if type(ids) is not list or any(type(v) is not int for v in ids) or len(ids) > 640:
            raise ValueError("完整重排问题超过640 tokens")
        return {"queries": values, "rerank_query_tokens_without_special": len(ids), "truncation": False}


class AsyncModelClient:
    packet = ModelClient.packet

    def __init__(self, settings, profile, http, store, instance, request_id):
        self.settings, self.profile, self.http, self.store = settings, profile, http, store
        self.instance, self.request_id = instance, request_id
        self.base = f"http://{settings.model_host}:{settings.model_port}"
        self.health = None
        self.calls = []

    async def request(self, method, path, *, timeout, payload=None, params=None):
        if timeout <= 0:
            raise TimeoutError("local model budget exhausted")
        limits = httpx.Timeout(timeout, connect=min(2, timeout), pool=min(2, timeout),
                               write=min(5, timeout))
        async with asyncio.timeout(timeout):
            async with self.http.stream(method, self.base + path,
                                        content=canonical(payload) if payload is not None else None,
                                        headers={"Content-Type": "application/json"},
                                        params=params, timeout=limits) as response:
                data = bytearray()
                async for block in response.aiter_bytes():
                    if len(data) + len(block) > MAX_RESPONSE:
                        raise ValueError("model response exceeds 512 KiB")
                    data.extend(block)
                return response.status_code, decode(bytes(data))

    async def ready(self, deadline):
        status, value = await self.request("GET", "/health/ready", timeout=min(2, deadline-time.monotonic()))
        if status != 200 or value.get("ready") is not True:
            raise ModelCallError("MODEL_NOT_READY", "本地模型服务尚未就绪。", {"health": value})
        validate_process(value["model_process"])
        for key in ("embedding_space_id", "execution_profile_id", "reranker_profile_id"):
            if value[key] != self.profile[key]:
                raise ValueError("model profile does not match the query generation")
        if type(value["service_instance_id"]) is not str:
            raise ValueError("model service identity is missing")
        self.health = value

    async def fence(self, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("query stage budget exhausted")
        await self.store.call("fence", self.instance, self.request_id)

    async def observe(self, registration, packet):
        await self.store.call("observe_operation", self.instance, self.request_id,
                              registration["operation_id"], packet)

    async def cancel_operation(self, registration):
        status, value = await self.request(
            "POST", "/operations/" + registration["operation_id"] + "/cancel", timeout=2,
            payload={"service_instance_id": registration["service_instance_id"],
                     "owner_id": registration["owner_id"]})
        if status != 200:
            raise ValueError("model cancellation status is unconfirmed")
        packet = self.packet(value, registration, tombstone_allowed=True)
        await self.observe(registration, packet)
        return packet

    async def call(self, operation, items, deadline, query=None):
        started = time.monotonic()
        deadline = min(deadline, started + 30)
        await self.fence(deadline)
        if self.health is None:
            await self.ready(deadline)
        payload = {"service_instance_id": self.health["service_instance_id"],
                   "owner_id": self.request_id, "operation_id": str(uuid4()),
                   "execution_profile_id": self.profile["execution_profile_id"],
                   "budget_ms": max(1, min(30000, int((deadline-time.monotonic())*1000))),
                   "items": items}
        if operation == "embed":
            payload["embedding_space_id"] = self.profile["embedding_space_id"]
        else:
            payload.update(reranker_profile_id=self.profile["reranker_profile_id"], query=query)
        registration = {key: payload[key] for key in ("service_instance_id", "operation_id", "owner_id", "budget_ms")}
        registration.update(model_process=self.health["model_process"], operation=operation,
                            ordered_item_ids=[item["item_id"] for item in items])
        if len(canonical(payload)) > 1024 * 1024:
            raise ValueError("model request exceeds 1 MiB")
        sent, packet = False, None
        call = {"registration": registration, "request_sha256": text_sha256(canonical(payload).decode()),
                "application_post_attempts": 0, "physical_send_count": "NOT_OBSERVED",
                "elapsed_seconds": None, "last_observed_operation": None}
        self.calls.append(call)
        try:
            await self.store.call("register_operation", self.instance, self.request_id, registration)
            await self.fence(deadline)
            sent = True
            call["application_post_attempts"] = 1
            status, value = await self.request("POST", "/" + operation, timeout=deadline-time.monotonic(),
                                               payload=payload)
            if status not in {200, 202}:
                raise ModelCallError("MODEL_REQUEST_REJECTED", "本地模型拒绝操作，不重发。",
                                     {"status": status, "response": value})
            packet = self.packet(value, registration)
            while packet["state"] not in TERMINAL:
                await self.fence(deadline)
                await asyncio.sleep(min(0.1, max(0, deadline-time.monotonic())))
                status, value = await self.request(
                    "GET", "/operations/" + registration["operation_id"],
                    timeout=deadline-time.monotonic(),
                    params={"service_instance_id": registration["service_instance_id"], "owner_id": self.request_id})
                if status != 200:
                    raise ValueError("model result status is unconfirmed")
                packet = self.packet(value, registration)
            await self.observe(registration, packet)
            call["last_observed_operation"] = packet
            await self.fence(deadline)
            if packet["state"] != "SUCCEEDED":
                raise ModelCallError("MODEL_OPERATION_" + packet["state"], "本地模型操作未成功完成。",
                                     {"operation": packet})
            return self.validate_result(operation, items, packet["result"])
        except BaseException as error:
            call["error_type"] = type(error).__name__
            call["last_observed_operation"] = packet
            if packet is not None:
                try:
                    await asyncio.shield(self.observe(registration, packet))
                except BaseException as observation_error:
                    call["observation_save_error"] = type(observation_error).__name__
            if (packet is None or packet["state"] not in TERMINAL
                         or packet["compute_inflight"] or packet["completion_unconfirmed"]):
                try:
                    call["cancellation"] = await asyncio.shield(self.cancel_operation(registration))
                except Exception as cancellation_error:
                    call["cancellation_error"] = type(cancellation_error).__name__
            raise
        finally:
            call["elapsed_seconds"] = time.monotonic()-started

    def validate_result(self, operation, inputs, result):
        if type(result) is not dict or set(result) != {"items", "trace"} or type(result["items"]) is not list:
            raise ValueError("query model result shape differs")
        if len(result["items"]) != len(inputs):
            raise ValueError("query model result cardinality differs")
        for source, item in zip(inputs, result["items"], strict=True):
            field = "dense" if operation == "embed" else "score"
            if type(item) is not dict or set(item) != {"item_id", field} or item["item_id"] != source["item_id"]:
                raise ValueError("query model result identity differs")
            if operation == "embed":
                vector_values(item["dense"])
            elif type(item["score"]) not in {int, float} or not math.isfinite(item["score"]):
                raise ValueError("reranker raw logit is not finite")
        trace = result["trace"]
        if operation == "embed":
            if (type(trace) is not dict or set(trace) != {"query_inputs", "model_compute_item_count", "cache_capacity"}
                    or trace["cache_capacity"] != 1024 or type(trace["query_inputs"]) is not list
                    or len(trace["query_inputs"]) != len(inputs)):
                raise ValueError("query cache trace shape differs")
            for source, item in zip(inputs, trace["query_inputs"], strict=True):
                if (type(item) is not dict or set(item) != {"item_id", "input_sha256", "cache_key", "cache_hit",
                                                         "cache_invalidated", "embedding_tokens", "reranker_tokens"}
                        or item["item_id"] != source["item_id"] or item["input_sha256"] != text_sha256(source["text"])
                        or item["cache_key"] != vector_key(self.profile, "query", source["text"])
                        or type(item["cache_hit"]) is not bool or type(item["cache_invalidated"]) is not bool
                        or any(type(item[key]) is not int or not 1 <= item[key] <= 256
                               for key in ("embedding_tokens", "reranker_tokens"))):
                    raise ValueError("query cache trace does not bind the exact input")
            if trace["model_compute_item_count"] != sum(not item["cache_hit"] for item in trace["query_inputs"]):
                raise ValueError("query cache compute count differs")
        else:
            if (type(trace) is not dict or set(trace) != {"query_tokens_without_special", "pairs", "score_type", "batch_size"}
                    or trace["score_type"] != "raw_logit" or trace["batch_size"] != len(inputs)
                    or type(trace["query_tokens_without_special"]) is not int
                    or not 0 <= trace["query_tokens_without_special"] <= 640
                    or type(trace["pairs"]) is not list or len(trace["pairs"]) != len(inputs)):
                raise ValueError("reranker trace shape differs")
            for source, pair in zip(inputs, trace["pairs"], strict=True):
                if (type(pair) is not dict or set(pair) != {"item_id", "document_tokens_without_special",
                                                         "pair_tokens_with_special", "production_pair_exact"}
                        or pair["item_id"] != source["item_id"] or pair["production_pair_exact"] is not True
                        or any(type(pair[key]) is not int or not 0 <= pair[key] <= 1792
                               for key in ("document_tokens_without_special", "pair_tokens_with_special"))):
                    raise ValueError("reranker actual pair trace differs")
        return result

    async def settle(self):
        """An unknown/active CPU operation retains its reader and global query slot."""
        unresolved = []
        for item in await self.store.call("operations", self.instance, self.request_id):
            registration, packet = item["registration"], item["observation"]
            if packet is not None and packet["state"] in TERMINAL and not packet["compute_inflight"] and not packet["completion_unconfirmed"]:
                continue
            process = registration["model_process"]
            if process_liveness(process["pid"], process["process_created_at"]) == "STOPPED":
                continue
            try:
                status, value = await self.request(
                    "GET", "/operations/" + registration["operation_id"], timeout=2,
                    params={"service_instance_id": registration["service_instance_id"], "owner_id": registration["owner_id"]})
                if status == 404:
                    # Absence is not a stop proof. Reserve the same operation's
                    # cancellation tombstone; never replay embed/rerank POSTs.
                    # Capacity/transport/ownership failures still retain the slot.
                    packet = await self.cancel_operation(registration)
                elif status == 200:
                    packet = self.packet(value, registration, tombstone_allowed=True)
                    await self.observe(registration, packet)
                else:
                    raise ValueError("operation not observed")
                if packet["state"] not in TERMINAL or packet["compute_inflight"] or packet["completion_unconfirmed"]:
                    unresolved.append(registration["operation_id"])
            except Exception:
                unresolved.append(registration["operation_id"])
        return unresolved
