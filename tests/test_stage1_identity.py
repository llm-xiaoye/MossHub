"""Stage 1 contract checks without containers, models, or cloud calls."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from raghub.contracts import EvidenceGroup, EvidenceUnit, RequestEvidence, RequestResult, UnderstandingResult
from raghub.db import IdentityStore, migrate, open_db, system_state


MIGRATIONS = Path(__file__).resolve().parents[1] / "backend/migrations"


class IdentityStoreTest(unittest.TestCase):
    def test_attempt_outbox_owner_and_recovery_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="raghub-stage1-") as root:
            with closing(open_db(Path(root) / "state.sqlite3", create=True)) as db:
                migrate(db, MIGRATIONS)
                state = system_state(db)
                instance = state["data_instance_id"]
                self.assertEqual(state["verification_status"], "VERIFIED")
                store = IdentityStore(db)
                digest = "a" * 64
                task, attempt, outbox = store.create_task(
                    kind="DOCUMENT", request_key="first-import", request_sha256=digest,
                    budget_scope_id="document-import",
                )
                self.assertEqual(
                    store.create_task(
                        kind="DOCUMENT", request_key="first-import", request_sha256=digest,
                        budget_scope_id="document-import",
                    ),
                    (task, attempt, outbox),
                )
                with self.assertRaises(ValueError):
                    store.create_task(
                        kind="DOCUMENT", request_key="first-import", request_sha256="b" * 64,
                        budget_scope_id="document-import",
                    )
                self.assertEqual(store.reserve_publish(outbox), (instance, task, attempt, 1))
                store.publish_outcome(outbox, confirmed=True)
                self.assertEqual(
                    store.claim_attempt(
                        data_instance_id=instance, task_id=task,
                        attempt_id=attempt, worker_id="worker-1",
                    ),
                    1,
                )
                with self.assertRaises(RuntimeError):
                    store.register_provider_attempt(
                        task_id=task, attempt_id=attempt, owner_epoch=2,
                        worker_id="worker-1", purpose="ocr", budget_scope_id="ocr-1",
                    )
                provider_attempt = store.register_provider_attempt(
                    task_id=task, attempt_id=attempt, owner_epoch=1,
                    worker_id="worker-1", purpose="ocr", budget_scope_id="ocr-1",
                )
                self.assertTrue(provider_attempt)
                new_instance = store.begin_recovery(recovery_id=str(uuid4()))
                self.assertNotEqual(new_instance, instance)
                self.assertEqual(system_state(db)["verification_status"], "VERIFYING")
                self.assertIsNone(
                    store.claim_attempt(
                        data_instance_id=instance, task_id=task,
                        attempt_id=attempt, worker_id="worker-2",
                    )
                )
                with self.assertRaises(RuntimeError):
                    store.register_provider_attempt(
                        task_id=task, attempt_id=attempt, owner_epoch=1,
                        worker_id="worker-1", purpose="ocr", budget_scope_id="ocr-1",
                    )


class UnderstandingContractTest(unittest.TestCase):
    def test_action_fields_are_exclusive(self) -> None:
        base = {
            "used_turn_ids": (), "used_summary_item_ids": (),
            "clarification_resolution": None,
        }
        self.assertEqual(
            UnderstandingResult(
                **base, main_action="retrieve", status="rewritten", query="什么是 BM25？",
                clarification=None, response_text=None,
            ).query,
            "什么是 BM25？",
        )
        with self.assertRaises(ValidationError):
            UnderstandingResult(
                **base, main_action="retrieve", status="rewritten", query=None,
                clarification=None, response_text="无证据的直接回答",
            )


class EvidenceAndResultTest(unittest.TestCase):
    def test_seed_group_limit_and_complete_evidence_budget(self) -> None:
        scope = dict(request_id=uuid4(), kb_id=uuid4(), generation_id=uuid4())
        unit = EvidenceUnit(
            support_unit_id="sentence-1", parent_id="a" * 64,
            processing_version_id=uuid4(), source_record_ids=("page-1",), text="正文句子。",
        )
        group = EvidenceGroup(**scope, units=(unit,) * 10)
        evidence = RequestEvidence(**scope, groups=(group,) * 8, serialized_token_count=8000)
        self.assertEqual(len(evidence.groups[0].units), 10)
        with self.assertRaises(ValidationError):
            RequestEvidence(**scope, groups=(group,) * 9, serialized_token_count=8000)
        with self.assertRaises(ValidationError):
            RequestEvidence(**scope, groups=(group,), serialized_token_count=8001)
        with self.assertRaises(ValidationError):
            EvidenceGroup(**scope, units=())
        foreign_group = group.model_copy(update={"kb_id": uuid4()})
        with self.assertRaises(ValidationError):
            RequestEvidence(**scope, groups=(foreign_group,), serialized_token_count=8000)

    def test_success_actions_require_their_corresponding_result(self) -> None:
        valid_pairs = (
            ("retrieve", "factual_answer", "answered"),
            ("clarify", "clarification", None),
            ("feature_limit", "feature_limit", None),
            ("respond", "simple_response", None),
            ("respond", "cancellation_confirmation", None),
        )
        for action, result, answer in valid_pairs:
            with self.subTest(action=action, result=result):
                RequestResult(
                    request_id=uuid4(), status="SUCCEEDED", main_action=action,
                    result_type=result, answer_status=answer,
                )
        for action, result in (("clarify", "feature_limit"), ("respond", "clarification")):
            with self.subTest(action=action, result=result), self.assertRaises(ValidationError):
                RequestResult(
                    request_id=uuid4(), status="SUCCEEDED", main_action=action,
                    result_type=result, answer_status=None,
                )
        # Failure diagnostics do not become a successful answer by pairing fields.
        RequestResult(
            request_id=uuid4(), status="FAILED", main_action="clarify",
            result_type="feature_limit", answer_status=None,
        )


if __name__ == "__main__":
    unittest.main()
