"""The stage-5 pipeline produces evidence; it does not generate an answer."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..indexing.storage import decode
from ..ingestion.store import BusinessError
from ..model_profiles import profiles
from ..worker_runtime import process_liveness
from .artifacts import GenerationArtifacts
from .contracts import normalized_queries
from .evidence import EvidenceAssembler
from .milvus import MilvusGateway
from .model_client import AsyncModelClient, QueryTokenGate
from .ranking import SEED_SELECTION_POLICY, fuse, order_reranked, rerank_batches, select_seeds
from .store import AsyncStore
from .tokenizer import EvidenceTokenizer

LOGGER = logging.getLogger(__name__)
RERANK_STAGE_SECONDS = 90


class RetrievalService:
    def __init__(self, settings, store_type=None):
        self.settings = settings
        self.profile = profiles(settings)
        self.db = AsyncStore(settings) if store_type is None else AsyncStore(settings, store_type)
        self.io = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag-query-artifacts")
        self.http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.milvus = MilvusGateway(settings)
        self.tasks, self.io_work, self.pending = {}, {}, {}
        self.drainer = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self.query_tokens, self.evidence_tokens = await loop.run_in_executor(
            self.io, lambda: (QueryTokenGate(self.settings), EvidenceTokenizer(self.settings)))
        self.assembler = EvidenceAssembler(self.evidence_tokens)
        self.drainer = asyncio.create_task(self.drain_loop(), name="rag-query-drain")

    async def file_work(self, request_id, function, *args):
        future = asyncio.get_running_loop().run_in_executor(self.io, function, *args)
        self.io_work.setdefault(request_id, []).append(future)
        # Cancellation stops adoption; the reader remains until actual I/O ends.
        return await asyncio.shield(future)

    async def fence(self, instance, request_id, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("180-second whole-request budget exhausted")
        await self.db.call("fence", instance, request_id)

    async def progress(self, instance, request_id, trace, stage, *, completed=None, total=None):
        value = {"stage": stage, "observed_at": time.time()}
        if completed is not None and total is not None:
            value.update(completed=completed, total=total)
        trace["progress"] = value
        await self.db.call("save_progress", instance, request_id, trace)

    async def submit(self, instance, session_id, request_id, body):
        received = time.monotonic()
        inputs = normalized_queries(body)
        admitted = await self.db.call("admit", instance, session_id, request_id, inputs)
        if admitted["new"]:
            task = asyncio.create_task(self.run(admitted["request"], received+180), name="rag-query-" + request_id)
            self.tasks[request_id] = task
            task.add_done_callback(lambda _task: self.tasks.pop(request_id, None))
        return await self.db.call("read", instance, request_id)

    async def run(self, row, deadline):
        request_id, instance = row["request_id"], row["data_instance_id"]
        inputs, snapshot = decode(row["input_json"]), decode(row["snapshot_json"])
        started = time.monotonic()
        model = AsyncModelClient(self.settings, self.profile, self.http, self.db, instance, request_id)
        trace = {"schema": "raghub.retrieval-trace.v1", "request_id": request_id,
                 "session_id": row["session_id"], "created_at": row["created_at"],
                 "scope": snapshot, "input": inputs, "model_profile": self.profile,
                 "budgets": {"whole_seconds": 180, "recall_seconds": 20, "rpc_seconds": 5,
                             "rerank_seconds": RERANK_STAGE_SECONDS, "model_operation_seconds": 30,
                             "recall_candidates": 80, "rerank_candidates": 40,
                             "seed_children": 8, "evidence_tokens": 8000},
                 "recall_routes": [], "model_calls": model.calls, "stage_timings": {},
                 "answer_generated": False, "algorithm_comparison_performed": False,
                 "timing_contract": {"deadline_clock": "time.monotonic", "asyncio_timeout_mode": "RELATIVE_REMAINING",
                     "started_monotonic": started, "deadline_monotonic": deadline,
                     "event_loop_time_observed": asyncio.get_running_loop().time(),
                     "event_loop_type": type(asyncio.get_running_loop()).__module__ + "." + type(asyncio.get_running_loop()).__name__}}
        state, error = "SUCCEEDED", None
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                await self.fence(instance, request_id, deadline)
                plan = await self.before_retrieval(row, inputs, snapshot, trace, deadline)
                inputs = plan["inputs"]
                if plan["retrieve"]:
                    prepare_start = time.monotonic()
                    trace["query_token_checks"] = await self.file_work(request_id, self.query_tokens.validate, inputs)
                    artifacts = await self.file_work(request_id, GenerationArtifacts, self.settings, snapshot, self.profile)
                    trace["artifact_reads"] = artifacts.reads
                    trace["stage_timings"]["prepare_seconds"] = time.monotonic()-prepare_start
                    await self.db.call("save_progress", instance, request_id, trace)
                    await self.fence(instance, request_id, deadline)
                    await self.progress(instance, request_id, trace, "RECALL")
                    recall_started = time.monotonic()
                    recall_deadline = min(deadline, recall_started+20)
                    count = len(inputs["unique_queries"])
                    limit = 40 if count == 1 else 20
    
                    async def lexical(query_id, query):
                        return await self.milvus.search(snapshot, "bm25", query_id, query, limit,
                                                        recall_deadline, trace["recall_routes"])
    
                    async def dense():
                        items = [{"item_id": f"Q{i+1}", "text": query, "input_kind": "query"}
                                 for i, query in enumerate(inputs["unique_queries"])]
                        embedded = await model.call("embed", items, recall_deadline)
                        trace["query_vector_trace"] = embedded["trace"]
                        async with asyncio.TaskGroup() as group:
                            for item in embedded["items"]:
                                group.create_task(self.milvus.search(
                                    snapshot, "dense", item["item_id"], item["dense"], limit,
                                    recall_deadline, trace["recall_routes"]))
    
                    async with asyncio.timeout(max(0.0, recall_deadline - time.monotonic())):
                        async with asyncio.TaskGroup() as group:
                            for index, query in enumerate(inputs["unique_queries"]):
                                group.create_task(lexical(f"Q{index+1}", query))
                            group.create_task(dense())
                    trace["stage_timings"]["recall_seconds"] = time.monotonic()-recall_started
                    await self.fence(instance, request_id, deadline)
                    fused = fuse(artifacts, trace["recall_routes"], count)
                    trace["fusion"] = {"formula": "query_weight*(0.5*dense_norm+0.5*bm25_norm)",
                                       "unique_count": len(fused), "candidates": fused}
                    await self.db.call("save_progress", instance, request_id, trace)
                    from .ranking import RERANK_TEXT_POLICY, rerank_passage
                    rerank_started = time.monotonic()
                    rerank_deadline = min(deadline, rerank_started+RERANK_STAGE_SECONDS)
                    batches, schedule = [], rerank_batches(fused)
                    model_inputs = []
                    rerank_total, completed = sum(len(batch) for batch in schedule), 0
                    await self.progress(instance, request_id, trace, "RERANK", completed=completed, total=rerank_total)
                    async with asyncio.timeout(max(0.0, rerank_deadline - time.monotonic())):
                        for batch in schedule:
                            await self.fence(instance, request_id, rerank_deadline)
                            items = [{"item_id": item["chunk_id"],
                                "text": rerank_passage(item["text"], artifacts.sources[item["source_record_id"]]["parts"])}
                                for item in batch]
                            model_inputs.extend(items)
                            batches.append(await model.call("rerank", items, rerank_deadline, inputs["rerank_query"]))
                            completed += len(batch)
                            await self.progress(instance, request_id, trace, "RERANK", completed=completed, total=rerank_total)
                    ranked = order_reranked(fused, batches)
                    trace["reranking"] = {"query": inputs["rerank_query"], "batches": batches, "candidates": ranked,
                        "text_policy": RERANK_TEXT_POLICY, "model_inputs": model_inputs,
                        "original_text_source": "fusion.candidates[].text",
                        "input_policy": "VALIDATED_COMPLETE_QUERY_V2", "batch_policy": "STABLE_STORED_LENGTH_ASCENDING_V2",
                        "length_basis": "STORED_CHILD_TOKEN_COUNT_PROXY_NOT_RERANK_TOKENIZER",
                        "candidate_selection_policy": "TOP_TEN_PER_ROUTE_OR_TWENTY_SHARED_THEN_FUSION_FILL_V1",
                        "scheduled_chunk_ids": [[item["chunk_id"] for item in batch] for batch in schedule],
                        "padding_proxy": sum(max(item["token_count"] for item in batch)*len(batch) for batch in schedule)}
                    trace["stage_timings"]["rerank_seconds"] = time.monotonic()-rerank_started
                    seeds, groups, decisions = select_seeds(artifacts, ranked, inputs["rerank_query"])
                    trace["seed_selection"] = {"seeds": seeds, "parent_groups": groups, "decisions": decisions,
                                               "equivalence_policy": "EXACT_PARSED_CONTENT_CONTEXT_V3",
                                               "selection_policy": dict(SEED_SELECTION_POLICY)}
                    await self.progress(instance, request_id, trace, "ASSEMBLE")
                    assemble_started = time.monotonic()
                    trace["evidence"] = await self.file_work(
                        request_id, self.assembler.assemble, request_id, artifacts, seeds, groups)
                    trace["stage_timings"]["assembly_seconds"] = time.monotonic()-assemble_started
                    await self.fence(instance, request_id, deadline)
                await self.after_retrieval(row, inputs, snapshot, trace, deadline)
                await self.fence(instance, request_id, deadline)
        except BaseException as caught:
            root = caught
            while isinstance(root, BaseExceptionGroup) and root.exceptions:
                root = root.exceptions[0]
            state = "CANCELLED" if getattr(root, "code", None) == "QUERY_CANCELLED" or isinstance(root, asyncio.CancelledError) else "FAILED"
            default_code = ("GENERATION_FAILED"
                if trace.get("chat",{}).get("stage") == "GENERATE" else "RETRIEVAL_FAILED")
            message = str(root).strip()
            if isinstance(root, TimeoutError):
                stage = trace.get("progress", {}).get("stage")
                names = {"UNDERSTAND": "理解问题", "RECALL": "检索资料", "RERANK": "资料排序",
                         "ASSEMBLE": "整理回答依据", "GENERATE": "生成回答", "VALIDATE": "核对回答"}
                name = names.get(stage, "本轮处理")
                progress = trace.get("progress", {})
                counts = (f"（已完成 {progress['completed']} / {progress['total']} 条候选）"
                          if stage == "RERANK" and "completed" in progress and "total" in progress else "")
                message = f"{name}超时{counts}。已发布资料和此前对话仍保留，本轮没有保存正式回答。"
            error = {"code": getattr(root, "code", default_code),
                     "error_type": type(root).__name__, "message": (message or "本轮处理停止，请查看失败阶段和技术详情。")[:1000]}
            trace["error"] = error
            await self.db.call("cancel", instance, request_id)
        trace["stage_timings"]["observed_elapsed_seconds"] = time.monotonic()-started
        trace["completion"] = {"requested_state": state, "error": error,
                               "result_adoption_requires_terminal_request": True}
        await self.db.call("save_progress", instance, request_id, trace)
        unresolved = await model.settle()
        unresolved.extend(await self.settle_related(request_id))
        files_pending = any(not future.done() for future in self.io_work.get(request_id, []))
        if unresolved or files_pending:
            await self.db.call("cancel", instance, request_id)
            trace["completion"]["unresolved_model_operations"] = unresolved
            trace["completion"]["artifact_io_pending"] = files_pending
            await self.db.call("save_progress", instance, request_id, trace)
            self.pending[request_id] = {"row": row, "trace": trace, "state": state, "error": error, "model": model,
                                        "deadline": deadline if state == "SUCCEEDED" else None}
            return
        try:
            await self.db.call("finish", instance, request_id, state, trace, error, deadline if state == "SUCCEEDED" else None)
        except Exception as caught:
            self.pending[request_id] = {"row": row, "trace": trace, "state": state, "error": error, "model": model,
                                        "deadline": deadline if state == "SUCCEEDED" else None}
            await self.report_finalization_error(row, "RUN_FINALIZATION", caught)
            return
        self.io_work.pop(request_id, None)

    async def before_retrieval(self, row, inputs, snapshot, trace, deadline):
        return {"retrieve": True, "inputs": inputs}

    async def after_retrieval(self, row, inputs, snapshot, trace, deadline):
        return None

    async def settle_related(self, request_id):
        return []

    async def report_finalization_error(self, row, phase, caught):
        try:
            LOGGER.error("Retrieval finalization blocked for %s at %s", row["request_id"], phase,
                         exc_info=(type(caught), caught, caught.__traceback__))
        except Exception:
            pass  # Logging failure must not block another request.
        diagnostic = {"code": getattr(caught, "code", "FINALIZATION_ERROR"),
                      "error_type": type(caught).__name__, "message": str(caught)[:1000]}
        try:
            await self.db.call("record_finalization_error", row["data_instance_id"], row["request_id"], phase, diagnostic)
        except Exception:
            try:
                LOGGER.exception("Could not persist finalization diagnostic for %s", row["request_id"])
            except Exception:
                pass

    async def drain_row(self, row):
        request_id, instance = row["request_id"], row["data_instance_id"]
        if request_id in self.tasks:
            return
        if request_id not in self.pending:
            liveness = process_liveness(row["owner_pid"], row["owner_created_at"])
            if liveness == "UNKNOWN" or (liveness == "LIVE" and row["owner_pid"] != os.getpid()):
                return
        # Inspect the first complete record before cancel or any completion
        # rewrite. A partial/unknown record raises and retains the active reader.
        stored = await self.db.call("stored_completion", instance, request_id)
        if stored is not None:
            model = (self.pending[request_id]["model"] if request_id in self.pending else
                     AsyncModelClient(self.settings, self.profile, self.http, self.db, instance, request_id))
            self.pending[request_id] = {"row": row, "trace": stored["trace"],
                "state": stored["intent"]["requested_state"], "error": stored["intent"]["error"],
                "deadline": stored["intent"]["deadline_monotonic"], "model": model}
        elif request_id not in self.pending:
            # Restart/lost owner confirms old operations; never replay recall or
            # a model POST. This fallback is only for a missing completion record.
            trace = decode(row["progress_json"]) if row["progress_json"] else {
                "schema": "raghub.retrieval-trace.v1", "request_id": request_id,
                "scope": decode(row["snapshot_json"]), "input": decode(row["input_json"]),
                "observed_stages": "NO_DURABLE_PROGRESS", "answer_generated": False}
            error = {"code": "RETRIEVAL_INTERRUPTED", "message": "执行进程已结束，仅确认旧操作停止，不自动重跑。"}
            await self.db.call("cancel", instance, request_id)
            self.pending[request_id] = {"row": row, "trace": trace, "state": "INTERRUPTED", "error": error, "deadline": None,
                "model": AsyncModelClient(self.settings, self.profile, self.http, self.db, instance, request_id)}
        item = self.pending[request_id]
        unresolved = await item["model"].settle()
        unresolved.extend(await self.settle_related(request_id))
        if unresolved or any(not future.done() for future in self.io_work.get(request_id, [])):
            return
        if stored is None:
            item["trace"]["completion"] = {"requested_state": item["state"], "error": item["error"],
                                               "old_operations_confirmed_stopped": True}
        await self.db.call("finish", instance, request_id, item["state"], item["trace"], item["error"], item["deadline"])
        self.pending.pop(request_id, None)
        self.io_work.pop(request_id, None)

    async def drain_once(self):
        rows = await self.db.call("active")
        for row in rows:
            try:
                await self.drain_row(row)
            except asyncio.CancelledError:
                raise
            except Exception as caught:
                await self.report_finalization_error(row, "DRAIN_FINALIZATION", caught)

    async def drain_loop(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Could not read the active retrieval queue; readers are retained")

    async def validate_references(self, instance, request_id, references):
        record = await self.db.call("read", instance, request_id)
        if record["state"] != "SUCCEEDED":
            raise BusinessError("REFERENCE_REQUEST_NOT_COMPLETE", "仅能引用本轮已完成并冻结的证据。")
        snapshot = record["snapshot"]
        whitelist = record["trace"]["evidence"]["support_unit_whitelist"]
        removed = await self.db.call("current_removals", instance, snapshot["kb_id"])
        keys = {"evidence_id", "support_unit_id", "kb_id", "generation_id", "document_id",
                "raw_version_id", "processing_version_id", "source_record_id"}
        for reference in references:
            if type(reference) is not dict or set(reference) != keys or any(type(v) is not str for v in reference.values()):
                raise BusinessError("REFERENCE_SHAPE_INVALID", "引用字段不符合本轮支持单元合同。", 422)
            if reference["kb_id"] != snapshot["kb_id"] or reference["generation_id"] != snapshot["generation_id"]:
                raise BusinessError("REFERENCE_SCOPE_MISMATCH", "引用来自其他知识库或索引代次，整条拒绝。")
            unit = whitelist.get(reference["support_unit_id"])
            if unit is None or any(reference[key] != unit[key] for key in keys):
                raise BusinessError("REFERENCE_NOT_IN_WHITELIST", "引用不是本轮实际发送的支持单元。")
            if reference["document_id"] in removed["removed_document_ids"]:
                raise BusinessError("REFERENCE_DOCUMENT_REMOVED", "资料已移除，不再开放其来源引用。")
        return {"request_id": request_id, "validated": True, "reference_count": len(references),
                "semantic_support_proven": False}

    async def close(self):
        if self.drainer is not None:
            self.drainer.cancel()
            await asyncio.gather(self.drainer, return_exceptions=True)
        for request_id, task in list(self.tasks.items()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        await self.milvus.close()
        await self.http.aclose()
        self.io.shutdown(wait=True, cancel_futures=False)
        await self.db.close()
