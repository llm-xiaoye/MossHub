"""Milvus writes a fresh collection; verified readers close its publication."""
from __future__ import annotations

import functools
import inspect
import json
import math
import time
from importlib.metadata import version
from types import MethodType
from uuid import UUID

from pymilvus import DataType, Function, FunctionType, MilvusClient

from ..model_profiles import canonical, text_sha256
from .vector_cache import vector_values

ANALYZER = {"tokenizer": {"type": "jieba", "dict": ["_default_"], "mode": "search", "hmm": True},
            "filter": ["lowercase"]}
DENSE_INDEX = {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}}
SPARSE_INDEX = {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25",
                "params": {"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75,
                           "drop_ratio_build": 0.0}}
HASH_FIELDS = ("chunk_id", "parent_id", "chunk_set_id", "embedding_space_id", "source_record_id", "text_sha256")
UUID_FIELDS = ("kb_id", "document_id", "raw_version_id", "processing_version_id", "generation_id")
TEXT_FIELDS = {**{name: 64 for name in HASH_FIELDS}, **{name: 36 for name in UUID_FIELDS},
               "content_kind": 32, "source_format": 16, "text": 65535}
METADATA_FIELDS = (*TEXT_FIELDS, "token_count", "chunk_ordinal")


def collection_name(kb_id, generation_id):
    for value in (kb_id, generation_id):
        if str(UUID(value)) != value or UUID(value).version != 4:
            raise ValueError("canonical UUID4 collection identity required")
    return "rag_k" + UUID(kb_id).hex + "_g" + UUID(generation_id).hex


class MilvusBuildError(RuntimeError):
    pass


class DedicatedClient(MilvusClient):
    """Only this worker handler loses SDK application retry decorators."""
    def __init__(self, uri, remaining):
        if version("pymilvus") != "2.6.17":
            raise MilvusBuildError("installed pymilvus source contract changed")
        self.remaining, self._bound_handler = remaining, None
        self.unwrapped_methods = []
        super().__init__(uri=uri, timeout=min(30.0, remaining()), dedicated=True)

    def _get_connection(self):
        handler = super()._get_connection()
        if handler is self._bound_handler:
            return handler
        for name, value in vars(type(handler)).items():
            if not inspect.isfunction(value):
                continue
            plain = inspect.unwrap(value)
            signature = inspect.signature(plain)
            if "timeout" not in signature.parameters:
                if plain is not value:
                    setattr(handler, name, MethodType(plain, handler))
                    self.unwrapped_methods.append(name)
                continue
            @functools.wraps(plain)
            def bounded(instance, *args, __plain=plain, __signature=signature, **kwargs):
                bound = __signature.bind(instance, *args, **kwargs)
                requested = bound.arguments.get("timeout")
                bound.arguments["timeout"] = min(30.0, self.remaining(),
                                                 float(requested) if requested is not None else 30.0)
                return __plain(*bound.args, **bound.kwargs)
            setattr(handler, name, MethodType(bounded, handler))
            if plain is not value:
                self.unwrapped_methods.append(name)
        self._bound_handler = handler
        return handler


def json_parameter(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def schema_contract(observed):
    if (observed["auto_id"] is not False or observed["enable_dynamic_field"] is not False
            or observed["consistency_level_name"] != "Strong"):
        raise MilvusBuildError("automatic IDs or dynamic fields are enabled")
    fields = observed["fields"]
    if len(fields) != len(TEXT_FIELDS) + 4:
        raise MilvusBuildError("Milvus field count differs")
    names = [field["name"] for field in fields]
    if len(set(names)) != len(names) or set(names) != set(TEXT_FIELDS) | {"token_count", "chunk_ordinal", "dense", "sparse"}:
        raise MilvusBuildError("Milvus fields differ")
    for field in fields:
        name, params = field["name"], field.get("params", {})
        expected_type = (DataType.VARCHAR if name in TEXT_FIELDS else
                         DataType.INT32 if name == "token_count" else DataType.INT64 if name == "chunk_ordinal" else
                         DataType.FLOAT_VECTOR if name == "dense" else DataType.SPARSE_FLOAT_VECTOR)
        if field["type"] != expected_type or bool(field.get("nullable", False)):
            raise MilvusBuildError("Milvus field type or nullability differs")
        if bool(field.get("is_primary", False)) != (name == "chunk_id"):
            raise MilvusBuildError("Milvus primary key differs")
        if name in TEXT_FIELDS and params.get("max_length") != TEXT_FIELDS[name]:
            raise MilvusBuildError("Milvus text limit differs")
        if name == "dense" and params.get("dim") != 1024:
            raise MilvusBuildError("Milvus dense dimension differs")
        if name == "text":
            if json_parameter(params.get("enable_analyzer")) is not True or json_parameter(params.get("analyzer_params")) != ANALYZER:
                raise MilvusBuildError("Milvus Jieba analyzer differs")
    functions = observed["functions"]
    if len(functions) != 1:
        raise MilvusBuildError("Milvus BM25 function count differs")
    function = functions[0]
    if (function["name"] != "text_bm25" or function["type"] != FunctionType.BM25
            or list(function["input_field_names"]) != ["text"] or list(function["output_field_names"]) != ["sparse"]):
        raise MilvusBuildError("Milvus BM25 function differs")


def index_contract(observed, expected):
    params = {key: json_parameter(value) for key, value in observed.items()}
    for key in ("index_type", "metric_type"):
        if params.get(key) != expected[key]:
            raise MilvusBuildError("Milvus index type or metric differs")
    nested = params.get("params", {})
    for key, value in expected["params"].items():
        actual = nested.get(key, params.get(key))
        if isinstance(value, (float, int)):
            if isinstance(actual, bool) or actual is None or float(actual) != value:
                raise MilvusBuildError("Milvus index numeric parameter differs: " + key)
        elif actual != value:
            raise MilvusBuildError("Milvus index parameter differs: " + key)


def rows_for(kb_id, generation_id, document_id, children, vectors):
    result = []
    for child in children:
        row = {key: child[key] for key in (*HASH_FIELDS, "raw_version_id", "processing_version_id",
                                          "content_kind", "source_format", "text", "token_count", "chunk_ordinal")}
        row.update(kb_id=kb_id, generation_id=generation_id, document_id=document_id,
                   dense=vector_values(vectors[child["chunk_id"]]["vector"])[0].tolist())
        if set(row) != set(METADATA_FIELDS) | {"dense"} or text_sha256(row["text"]) != row["text_sha256"]:
            raise MilvusBuildError("Milvus row is not bound to its complete text")
        for key, limit in TEXT_FIELDS.items():
            if type(row[key]) is not str or not row[key] or len(row[key].encode("utf-8")) > limit:
                raise MilvusBuildError("Milvus text value exceeds its bound")
        for key in HASH_FIELDS:
            if len(row[key]) != 64 or any(char not in "0123456789abcdef" for char in row[key]):
                raise MilvusBuildError("Milvus hash identity format differs")
        for key in UUID_FIELDS:
            if str(UUID(row[key])) != row[key] or UUID(row[key]).version != 4:
                raise MilvusBuildError("Milvus UUID identity format differs")
        for key in ("token_count", "chunk_ordinal"):
            if type(row[key]) is not int or row[key] < 0:
                raise MilvusBuildError("Milvus integer metadata differs")
        result.append(row)
    return result


class CollectionBuild:
    def __init__(self, settings, name, fence, record, *, budget_seconds=900):
        self.settings, self.name, self.fence, self.record = settings, name, fence, record
        self.deadline = time.monotonic() + budget_seconds
        self.client = DedicatedClient(settings.milvus_uri, self.remaining)
        self.record("MILVUS_CLIENT_BOUND", {"pymilvus": version("pymilvus"),
                    "sdk_retry_decorators_removed": sorted(self.client.unwrapped_methods),
                    "physical_rpc_retry_count": "NOT_OBSERVED"})
        self.expected = {}

    def remaining(self):
        self.fence()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MilvusBuildError("Milvus stage budget exhausted")
        return remaining

    def call(self, method, **kwargs):
        self.remaining()
        result = getattr(self.client, method)(timeout=min(30.0, self.remaining()), **kwargs)
        self.remaining()
        return result

    def create(self):
        if self.call("has_collection", collection_name=self.name):
            raise MilvusBuildError("new generation collection already exists")
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        for name, length in TEXT_FIELDS.items():
            options = {"max_length": length, "is_primary": name == "chunk_id"}
            if name == "text":
                options.update(enable_analyzer=True, analyzer_params=ANALYZER)
            schema.add_field(name, DataType.VARCHAR, **options)
        schema.add_field("token_count", DataType.INT32)
        schema.add_field("chunk_ordinal", DataType.INT64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="text_bm25", input_field_names=["text"],
                                     output_field_names=["sparse"], function_type=FunctionType.BM25))
        self.call("create_collection", collection_name=self.name, schema=schema, consistency_level="Strong")
        schema_contract(self.call("describe_collection", collection_name=self.name))
        self.record("MILVUS_COLLECTION_CREATED", {"collection_name": self.name, "consistency_level": "Strong"})

    def upsert(self, rows):
        first = 0
        while first < len(rows):
            batch = []
            for row in rows[first:first + 128]:
                if row["chunk_id"] in self.expected:
                    raise MilvusBuildError("duplicate child ID across generation")
                if len(canonical(batch + [row])) > 4 * 1024 * 1024:
                    break
                batch.append(row)
            if not batch:
                raise MilvusBuildError("one Milvus row exceeds the logical JSON batch budget")
            self.record("MILVUS_UPSERT_SUBMIT", {"collection_name": self.name, "row_count": len(batch),
                        "logical_json_bytes": len(canonical(batch)), "application_send_ordinal": 1,
                        "chunk_ids": [row["chunk_id"] for row in batch]})
            response = self.call("upsert", collection_name=self.name, data=batch)
            returned_ids = list(response.get("ids", []))
            if response.get("upsert_count") != len(batch) or returned_ids != [r["chunk_id"] for r in batch]:
                raise MilvusBuildError("Milvus upsert acknowledgement differs")
            acknowledgement = {**response, "ids": returned_ids}
            self.record("MILVUS_UPSERT_ACK", {"collection_name": self.name, "response": acknowledgement})
            self.expected.update({row["chunk_id"]: {key: row[key] for key in METADATA_FIELDS} for row in batch})
            first += len(batch)

    def complete(self, probe_row):
        self.call("flush", collection_name=self.name)
        indexes = MilvusClient.prepare_index_params()
        indexes.add_index("dense", index_name="dense_hnsw", **DENSE_INDEX)
        indexes.add_index("sparse", index_name="sparse_bm25", **SPARSE_INDEX)
        self.call("create_index", collection_name=self.name, index_params=indexes, sync=False)
        observed_indexes = {}
        for name, expected in (("dense_hnsw", DENSE_INDEX), ("sparse_bm25", SPARSE_INDEX)):
            while True:
                observation = self.call("describe_index", collection_name=self.name, index_name=name)
                index_contract(observation, expected)
                if observation["index_name"] != name or observation["field_name"] != ("dense" if name == "dense_hnsw" else "sparse"):
                    raise MilvusBuildError("Milvus index field binding differs")
                self.record("MILVUS_INDEX_OBSERVED", {"index_name": name, "raw": observation})
                if observation["state"] == "Finished":
                    observed_indexes[name] = observation
                    break
                if observation["state"] == "Failed":
                    raise MilvusBuildError("Milvus index reported Failed")
                time.sleep(min(0.5, self.remaining()))
        self.call("load_collection", collection_name=self.name, _async=True)
        while True:
            load = self.call("get_load_state", collection_name=self.name)
            state = getattr(load["state"], "name", str(load["state"]))
            self.record("MILVUS_LOAD_OBSERVED", {"state": state})
            if state == "Loaded":
                break
            time.sleep(min(0.5, self.remaining()))
        schema_contract(self.call("describe_collection", collection_name=self.name))
        iterator = self.call("query_iterator", collection_name=self.name, batch_size=256,
                             output_fields=list(METADATA_FIELDS), consistency_level="Strong")
        actual = {}
        try:
            while True:
                iterator._timeout = min(30.0, self.remaining())
                batch = iterator.next()
                self.remaining()
                if not batch:
                    break
                for row in batch:
                    if row["chunk_id"] in actual:
                        raise MilvusBuildError("duplicate Strong metadata row")
                    actual[row["chunk_id"]] = row
                    if len(actual) > len(self.expected):
                        raise MilvusBuildError("extra Strong metadata row")
        finally:
            iterator.close()
        if actual != self.expected:
            raise MilvusBuildError("Strong metadata does not close the planned generation")
        from pymilvus.client.types import AnalyzeResult
        sdk_analyzer = self.call("run_analyzer", texts=[probe_row["text"]], collection_name=self.name,
                                 field_name="text", with_hash=False, with_detail=False)
        if (type(sdk_analyzer) is not list or len(sdk_analyzer) != 1
                or not isinstance(sdk_analyzer[0], AnalyzeResult)):
            raise MilvusBuildError("Milvus analyzer result shape differs")
        if type(sdk_analyzer[0].tokens) is not list:
            raise MilvusBuildError("Milvus analyzer token container differs")
        tokens = list(sdk_analyzer[0].tokens)
        if any(type(token) is not str for token in tokens):
            raise MilvusBuildError("Milvus analyzer token type differs")
        if not any(token.strip() for token in tokens):
            raise MilvusBuildError("real text has no Jieba terms for the BM25 probe")
        analyzer = [tokens]
        probes = {}
        for name, data, params in (("dense", [probe_row["dense"]], {"metric_type": "COSINE", "params": {"ef": 64}}),
                                   ("sparse", [probe_row["text"]], {"metric_type": "BM25", "params": {"drop_ratio_search": 0.0}})):
            sdk_hits = self.call("search", collection_name=self.name, data=data, anns_field=name,
                                 search_params=params, limit=1, output_fields=list(METADATA_FIELDS),
                                 consistency_level="Strong")
            raw_hits = [[dict(hit) for hit in group] for group in sdk_hits]
            self.record("MILVUS_READABILITY_OBSERVED", {"route": name, "raw_hits": raw_hits})
            hits = []
            for group in raw_hits:
                normalized = []
                for raw_hit in group:
                    primary_key = raw_hit["chunk_id"]
                    entity = dict(raw_hit["entity"])
                    if "chunk_id" in entity and entity["chunk_id"] != primary_key:
                        raise MilvusBuildError("search primary key and entity differ")
                    entity["chunk_id"] = primary_key
                    normalized.append({"id": primary_key, "distance": float(raw_hit["distance"]), "entity": entity})
                hits.append(normalized)
            if len(hits) != 1 or not hits[0] or hits[0][0]["id"] not in self.expected:
                raise MilvusBuildError("real " + name + " readability probe did not reach this generation")
            hit = hits[0][0]
            if any(hit["entity"].get(key) != self.expected[hit["id"]][key] for key in METADATA_FIELDS):
                raise MilvusBuildError("readability probe returned different metadata")
            if not math.isfinite(float(hit["distance"])):
                raise MilvusBuildError("readability probe score is nonfinite")
            probes[name] = hits
        segments = self.client._get_connection().get_query_segment_info(self.name, timeout=min(30.0, self.remaining()))
        segment_rows = []
        for info in segments:
            row = {}
            for field in info.DESCRIPTOR.fields:
                value = getattr(info, field.name)
                row[field.name] = list(value) if field.is_repeated else value
            segment_rows.append(row)
        self.remaining()
        result = {"collection_name": self.name, "row_count": len(actual),
                  "metadata_sha256": text_sha256(canonical([actual[key] for key in sorted(actual)]).decode("utf-8")),
                  "indexes": observed_indexes, "load_state": state, "segments": segment_rows,
                  "analyzer_output": analyzer, "readability_probes": probes,
                  "all_rows_hnsw_graph_coverage": "NOT_PROVEN",
                  "probe_hnsw_execution": "NOT_PROVEN", "algorithm_effect": "NOT_EVALUATED"}
        self.record("MILVUS_READINESS_CLOSED", result)
        return result

    def close(self):
        self.client.close()
