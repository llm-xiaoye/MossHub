"""Two bounded online RPC slots; SDK result serialization belongs to the product."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from importlib.metadata import version
from types import MethodType

from pymilvus import AsyncMilvusClient

from ..ingestion.archive import uuid_text
from ..indexing.milvus_store import METADATA_FIELDS
from ..model_profiles import canonical, text_sha256


class QueryMilvusClient(AsyncMilvusClient):
    def __init__(self, uri):
        if version("pymilvus") != "2.6.17":
            raise ValueError("locked async SDK source contract changed")
        self._query_connection_lock = asyncio.Lock()
        self._query_bound_handler = None
        self.search_retry_unwrapped = False
        super().__init__(uri=uri, timeout=5, dedicated=True)

    async def _get_connection(self):
        async with self._query_connection_lock:
            handler = await super()._get_connection()
            if handler is not self._query_bound_handler:
                original = type(handler).search
                plain = inspect.unwrap(original)
                if not inspect.iscoroutinefunction(plain):
                    raise ValueError("async search handler contract changed")
                handler.search = MethodType(plain, handler)
                self.search_retry_unwrapped = plain is not original
                if not self.search_retry_unwrapped:
                    raise ValueError("expected SDK search retry decorator is missing")
                self._query_bound_handler = handler
            return handler


def filter_expression(snapshot):
    for value in (snapshot["kb_id"], snapshot["generation_id"]):
        uuid_text(value)
    removed = snapshot["removal_snapshot"]["removed_document_ids"]
    if type(removed) is not list or removed != sorted(set(removed)):
        raise ValueError("removal snapshot is not an ordered unique list")
    for value in removed:
        uuid_text(value)
    expression = "kb_id == " + json.dumps(snapshot["kb_id"]) + " and generation_id == " + json.dumps(snapshot["generation_id"])
    if removed:
        expression += " and document_id not in " + json.dumps(removed)
    return expression


def serialize_search_result(value, limit):
    # Locked 2.6.17 SearchResult/Hits are list subclasses; each Hit is a dict
    # whose top-level primary-key field is the collection's real chunk_id.
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], list) or len(value[0]) > limit:
        raise ValueError("SDK must return one bounded query result list")
    results, shapes = [], []
    seen = set()
    for hit in value[0]:
        if not isinstance(hit, dict) or set(hit) != {"chunk_id", "distance", "entity"}:
            raise ValueError("locked SDK hit fields changed")
        raw = dict(hit)
        entity, cid, score = raw["entity"], raw["chunk_id"], raw["distance"]
        if (type(cid) is not str or cid in seen or type(score) not in {int, float}
                or not math.isfinite(score) or not isinstance(entity, dict)):
            raise ValueError("SDK primary key, score or entity is invalid")
        seen.add(cid)
        entity = dict(entity)
        primary_projected = "chunk_id" not in entity
        if primary_projected:
            entity["chunk_id"] = cid
        if set(entity) != set(METADATA_FIELDS) or entity["chunk_id"] != cid:
            raise ValueError("SDK entity metadata fields changed")
        result = {"id": cid, "distance": float(score), "entity": entity}
        canonical(result)  # Reject non-JSON / nonfinite values before adopting.
        results.append(result)
        shapes.append({"hit_type": type(hit).__module__ + "." + type(hit).__name__,
                       "primary_key_field": "chunk_id", "primary_key_projected_into_entity": primary_projected})
    return results, {"result_type": type(value).__module__ + "." + type(value).__name__,
                     "hits_type": type(value[0]).__module__ + "." + type(value[0]).__name__,
                     "hits": shapes}


class MilvusGateway:
    def __init__(self, settings):
        self.client = QueryMilvusClient(settings.milvus_uri)
        self.slots = asyncio.Semaphore(2)

    async def close(self):
        await self.client.close()

    async def search(self, snapshot, route, query_id, data, limit, deadline, trace):
        started = time.monotonic()
        rpc_deadline = min(deadline, started + 5)
        expression = filter_expression(snapshot)
        record = {"query_id": query_id, "route": route, "limit": limit,
                  "kb_id": snapshot["kb_id"], "generation_id": snapshot["generation_id"],
                  "removal_snapshot": snapshot["removal_snapshot"], "filter": expression,
                  "consistency_level": "Strong", "application_search_attempts": 0,
                  "physical_rpc_count": "NOT_OBSERVED", "sdk_application_retry": False,
                  "input_sha256": text_sha256(canonical(data).decode()), "status": "WAITING",
                  "elapsed_seconds": None, "hits": []}
        trace.append(record)
        try:
            async with asyncio.timeout(max(0, rpc_deadline-time.monotonic())):
                async with self.slots:
                    record["slot_wait_seconds"] = time.monotonic()-started
                    record["status"] = "RUNNING"
                    record["application_search_attempts"] = 1
                    field = "dense" if route == "dense" else "sparse"
                    params = ({"metric_type": "COSINE", "params": {"ef": max(64, limit)}}
                              if route == "dense" else {"metric_type": "BM25", "params": {}})
                    raw = await self.client.search(
                        collection_name=snapshot["generation"]["collection_name"], data=[data],
                        anns_field=field, search_params=params, filter=expression, limit=limit,
                        output_fields=list(METADATA_FIELDS), consistency_level="Strong",
                        timeout=max(0.001, rpc_deadline-time.monotonic()))
                    hits, shapes = serialize_search_result(raw, limit)
                    if len(canonical(hits)) > 8 * 1024 * 1024:
                        raise ValueError("bounded search metadata exceeds 8 MiB")
                    record.update(status="SUCCEEDED", hits=hits, sdk_result_shapes=shapes,
                                  search_params=params)
                    return record
        except BaseException as error:
            record.update(status="FAILED", error_type=type(error).__name__, error=str(error)[:400])
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic()-started
