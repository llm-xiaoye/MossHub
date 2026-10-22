"""Regression checks for query identity, complete candidate scoring and conservative equivalence."""
from types import SimpleNamespace
import unittest
from raghub.retrieval.contracts import SearchRequest, normalized_queries
from raghub.retrieval.ranking import rerank_batches, order_reranked, select_seeds

class RetrievalOptimizationChecks(unittest.TestCase):
    def test_rewrite_drives_reranking_and_both_queries_recall(self):
        original="那他的国家呢？"
        complete="Preston Lozano,Dr 的 Country 是什么？"
        result=normalized_queries(SearchRequest(effective_original_query=original,rewritten_query=complete))
        self.assertEqual(result["unique_queries"],[original,complete])
        self.assertEqual(result["rerank_query"],complete)

    def test_length_batches_score_every_candidate_once_and_restore_order(self):
        candidates=[{"chunk_id":str(i),"token_count":length,"fusion_rank":i+1} for i,length in enumerate([900,12,800,10,700,20,600,18,50])]
        batches=rerank_batches(candidates)
        self.assertEqual(sorted(item["chunk_id"] for batch in batches for item in batch),sorted(item["chunk_id"] for item in candidates))
        self.assertTrue(all(len(batch)<=4 for batch in batches))
        mixed=sum(max(item["token_count"] for item in candidates[i:i+4])*len(candidates[i:i+4]) for i in range(0,len(candidates),4))
        grouped=sum(max(item["token_count"] for item in batch)*len(batch) for batch in batches)
        self.assertLess(grouped,mixed)
        outputs=[{"items":[{"item_id":item["chunk_id"],"score":float(item["fusion_rank"])} for item in batch]} for batch in batches]
        self.assertEqual([item["chunk_id"] for item in order_reranked(candidates,outputs)],list(reversed([item["chunk_id"] for item in candidates])))
        with self.assertRaises(ValueError):order_reranked(candidates,outputs[:-1])
        with self.assertRaises(ValueError):order_reranked(candidates,outputs+outputs[:1])

    def test_aliases_keep_sources_but_different_conditions_and_unknown_context_stay(self):
        parents={str(i):{"text":"相同正文","heading_scope":["适用范围"]} for i in range(4)}
        artifacts=SimpleNamespace(parents=parents,equivalence_contexts={
            "0":{"sha256":"bucket","serialized":"完整资料：条件A"},
            "1":{"sha256":"bucket","serialized":"完整资料：条件A"},
            "2":{"sha256":"bucket","serialized":"完整资料：条件B"}})
        ranked=[{"chunk_id":str(i),"parent_id":str(i),"text":"相同正文","equivalence_key":"legacy"+str(i),
                 "processing_version_id":"pv"+str(i),"raw_version_id":"raw"+str(i),"document_id":"doc"+str(i),
                 "content_kind":"paragraph","rerank_rank":i+1,"fusion_rank":i+1,"reranker_raw_logit":4-i} for i in range(4)]
        seeds,_,decisions=select_seeds(artifacts,ranked)
        self.assertEqual([item["chunk_id"] for item in seeds],["0","2","3"])
        alias=next(item for item in decisions if item["chunk_id"]=="1")
        self.assertEqual(alias["equivalent_to"],"0")
        self.assertEqual(alias["source"]["document_id"],"doc1")
        self.assertFalse(alias["sent_as_support_unit"])

class ExplicitCancelQueryChecks(unittest.TestCase):
    def test_cancel_then_new_query_retains_raw_intervals(self):
        from raghub.chat.query import knowledge_view
        for new_query in ("改查 Joanna Bender 的公司。", "改为查询文档更新办法。", "改成查询订阅日期。"):
            with self.subTest(new_query=new_query):
                raw = "取消上一个问题。" + new_query
                view = knowledge_view(raw)
                self.assertTrue(view["boundary_reliable"])
                self.assertEqual(view["control_intent"], "cancel")
                self.assertEqual(view["effective_original_query"], new_query)
                span = view["knowledge_intervals"][0]
                self.assertEqual(raw[span["start"]:span["end"]], new_query)
        unresolved = knowledge_view("不要取消上一个问题。改查另一个人的记录。")
        self.assertFalse(unresolved["boundary_reliable"])

class ParsedContentEquivalenceChecks(unittest.TestCase):
    def test_copy_provenance_does_not_hide_content_or_applicability(self):
        from raghub.retrieval.artifacts import parsed_content_equivalence
        def context(filename, revision="V1", body="标准模式超时12秒", extra_note="仅标准模式适用"):
            supplement = SimpleNamespace(adapter="markdown", config_sha256="config", model_lock_sha256="model",
                scope={"selected":"DOCUMENT_BODY"}, quality=[], images=[],
                format_details={"format":"md", "source_observation":{
                    "filename":filename, "raw_version_id":filename, "acquisition_attempt_manifest":filename,
                    "relative_path":filename, "source_root_id":filename, "unchanged":False,
                    "directory_context":None, "provenance":{"source_revision":revision,"source_date":"2026-10-12"}}},
                fields=[SimpleNamespace(field_pointer="/texts/0/text", original_text=body, cleaned_text=body),
                        SimpleNamespace(field_pointer="/texts/1/text", original_text=extra_note, cleaned_text=extra_note)])
            parents=[{"text":body,"body":body,"content_kind":"paragraph","heading_scope":["服务说明"]}]
            return parsed_content_equivalence(supplement, parents)
        original=context("a.md")
        self.assertEqual(original,context("reprint.md"))
        self.assertNotEqual(original,context("a.md",revision="V2"))
        self.assertNotEqual(original,context("a.md",body="受限模式超时30秒"))
        self.assertNotEqual(original,context("a.md",extra_note="仅高峰期适用"))

class ModelCancellationSettlementChecks(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        from unittest.mock import AsyncMock
        from raghub.retrieval.model_client import AsyncModelClient
        self.registration = {
            "service_instance_id": "model-instance", "operation_id": "registered-operation",
            "owner_id": "question-owner", "model_process": {"pid": 1234, "process_created_at": 1.0},
            "operation": "rerank", "budget_ms": 1000, "ordered_item_ids": ["candidate-a"],
        }
        profile = {"embedding_space_id": "embedding", "execution_profile_id": "execution",
                   "reranker_profile_id": "reranker"}
        self.packet = {**self.registration, **profile, "operation": None, "budget_ms": None,
                       "ordered_item_ids": [], "state": "CANCELLED", "tombstone": True,
                       "compute_inflight": False, "completion_unconfirmed": False,
                       "result": None, "error": {"code": "CANCEL_TOMBSTONE"}}
        store = SimpleNamespace(call=AsyncMock(side_effect=[
            [{"registration": self.registration, "observation": None}], None]))
        client = AsyncModelClient(SimpleNamespace(model_host="127.0.0.1", model_port=1),
                                  profile, None, store, "data-instance", "question-owner")
        client.request = AsyncMock()
        return client

    async def test_missing_operation_records_cancel_tombstone_without_replaying_compute(self):
        from unittest.mock import patch
        client = self.make_client()
        client.request.side_effect = [(404, {"code": "OPERATION_NOT_FOUND"}), (200, self.packet)]
        with patch("raghub.retrieval.model_client.process_liveness", return_value="RUNNING"):
            self.assertEqual(await client.settle(), [])
        calls = client.request.await_args_list
        self.assertEqual([(call.args[0], call.args[1]) for call in calls], [
            ("GET", "/operations/registered-operation"),
            ("POST", "/operations/registered-operation/cancel")])
        self.assertEqual(calls[1].kwargs["payload"],
                         {"service_instance_id": "model-instance", "owner_id": "question-owner"})
        self.assertEqual(client.store.call.await_args_list[-1].args,
                         ("observe_operation", "data-instance", "question-owner",
                          "registered-operation", self.packet))

    async def test_unconfirmed_cancellation_retains_operation_and_does_not_retry(self):
        from unittest.mock import patch
        client = self.make_client()
        client.request.side_effect = [(404, {}), (503, {"code": "CANCEL_NOT_RECORDED"})]
        with patch("raghub.retrieval.model_client.process_liveness", return_value="RUNNING"):
            self.assertEqual(await client.settle(), ["registered-operation"])
        self.assertEqual(client.request.await_count, 2)
        self.assertEqual(client.store.call.await_count, 1)

    async def test_identity_rejection_does_not_send_cancellation(self):
        from unittest.mock import patch
        for status in (403, 409):
            with self.subTest(status=status):
                client = self.make_client()
                client.request.return_value = (status, {})
                with patch("raghub.retrieval.model_client.process_liveness", return_value="RUNNING"):
                    self.assertEqual(await client.settle(), ["registered-operation"])
                self.assertEqual(client.request.await_count, 1)
                self.assertEqual(client.request.await_args.args[0], "GET")

    async def test_wrong_owner_or_unconfirmed_compute_cannot_release_operation(self):
        from unittest.mock import patch
        for change in ({"owner_id": "other-owner"}, {"compute_inflight": True},
                       {"completion_unconfirmed": True}):
            with self.subTest(change=change):
                client = self.make_client()
                client.request.side_effect = [(404, {}), (200, {**self.packet, **change})]
                with patch("raghub.retrieval.model_client.process_liveness", return_value="RUNNING"):
                    self.assertEqual(await client.settle(), ["registered-operation"])
                self.assertEqual(client.store.call.await_count, 1)

    async def test_observed_terminal_operation_does_not_send_another_cancel(self):
        from unittest.mock import patch
        client = self.make_client()
        client.request.return_value = (200, self.packet)
        with patch("raghub.retrieval.model_client.process_liveness", return_value="RUNNING"):
            self.assertEqual(await client.settle(), [])
        self.assertEqual(client.request.await_count, 1)
        self.assertEqual(client.request.await_args.args[0], "GET")

if __name__=="__main__":unittest.main()
