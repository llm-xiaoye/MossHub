"""Local fault-injection units only; no real cloud, multi-turn or Ragas credit."""
from __future__ import annotations

import asyncio
import copy
import json
import runpy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from raghub.config import Settings
from raghub.chat.service import ChatService
from raghub.ingestion.store import BusinessError
from raghub.retrieval.model_client import QueryTokenGate
from raghub.retrieval.tokenizer import EvidenceTokenizer


class Stage8FailureBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = Settings.load()
        cls.query_tokens = QueryTokenGate(settings)
        cls.evidence_tokens = EvidenceTokenizer(settings)

    def test_understanding_failure_stops_fact_retrieval_and_generation(self):
        # Only the cloud boundary throws; query/history/understanding logic is real.
        question = '他在哪个国家，订阅日期是哪天？国家按 CSV 的原值回答。'
        context = {
            'request': {'question': question, 'history_boundary': 0},
            'conversation': {'conversation_id': 'local-conversation'},
            'turns': [], 'summary': None, 'awaiting': None,
            'clarification_chain': None, 'source_message_order': {},
        }
        before = copy.deepcopy(context)
        calls = []

        async def store_call(name, *args):
            calls.append(name)
            if name == 'context':
                return copy.deepcopy(context)
            if name in {'save_history', 'fence'}:
                return None
            raise AssertionError('unexpected local store action: ' + name)

        service = ChatService.__new__(ChatService)
        service.db = SimpleNamespace(call=store_call)
        service.query_tokens = self.query_tokens
        service.evidence_tokens = self.evidence_tokens
        service.io_work = {}
        service.flash = SimpleNamespace(call=AsyncMock(side_effect=httpx.ConnectTimeout('local injected timeout')))
        service.after_retrieval = AsyncMock(side_effect=AssertionError('generation must not run'))
        trace = {}
        row = {'request_id': 'local-request', 'data_instance_id': 'local-instance',
               'session_id': 'local-conversation'}
        with ThreadPoolExecutor(max_workers=1) as executor:
            service.io = executor
            with self.assertRaises(BusinessError) as caught:
                asyncio.run(service.before_retrieval(row, {}, {}, trace, time.monotonic() + 60))
        self.assertEqual(caught.exception.code, 'UNDERSTANDING_FAILED')
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectTimeout)
        self.assertEqual(service.flash.call.await_count, 1)
        self.assertEqual(service.flash.call.await_args.args[2], 'understand')
        service.after_retrieval.assert_not_awaited()
        self.assertEqual(trace['chat']['understanding_failure']['type'], 'ConnectTimeout')
        self.assertEqual(trace['chat']['understanding_failure']['disposition'], 'STOP_FACTUAL_RETRIEVAL')
        self.assertNotIn('retrieval_input', trace['chat'])
        self.assertNotIn('understanding', trace['chat'])
        self.assertNotIn('formal_result', trace['chat'])
        self.assertEqual(trace['chat']['history_snapshot']['turns'], [])
        self.assertIsNone(trace['chat']['history_snapshot']['awaiting'])
        self.assertEqual(context, before)
        self.assertEqual(calls, ['context', 'save_history', 'fence'])
        print(json.dumps({'case': 'understanding_failure',
            'scope': 'LOCAL_FAULT_INJECTION_UNIT_NO_CLOUD_OR_FORMAL_CREDIT',
            'error_code': caught.exception.code,
            'cloud_boundary_call_count': service.flash.call.await_count,
            'disposition': trace['chat']['understanding_failure']['disposition']}))

    def test_failed_predecessor_skips_followup_and_independent_case_continues(self):
        driver_path = Path(__file__).resolve().parents[1] / 'scripts' / 'stage8_eval_v8.py'
        driver = runpy.run_path(str(driver_path), run_name='stage8_local_driver_unit')
        factual_gate = driver['_factual_continuation']
        cases = [
            {'case_id': 'FIRST', 'conversation_group': 'dependent'},
            {'case_id': 'FOLLOW', 'conversation_group': 'dependent'},
            {'case_id': 'INDEPENDENT', 'conversation_group': 'independent'},
        ]
        submitted = []
        with tempfile.TemporaryDirectory(prefix='rg-s8-dep-', dir='/tmp') as scratch:
            run = SimpleNamespace(
                root=Path(scratch) / 'questions',
                plan={'cases': cases, 'recover_question_requests': {},
                      'generation': {'scope': 'LOCAL_UNIT_NO_GENERATION'}, 'kb_id': 'local-kb'},
                plan_identity={'scope': 'LOCAL_UNIT_NOT_A_BOUND_PRODUCT_PLAN'},
                conversations={}, case_observations=[],
                client=SimpleNamespace(close=Mock()), request=Mock(return_value={}),
            )
            run.prepare = lambda: run.root.mkdir(exist_ok=False)

            def ask(case):
                submitted.append(case['case_id'])
                conversation = run.conversations.setdefault(case['conversation_group'], 'local-' + case['conversation_group'])
                if case['case_id'] == 'FIRST':
                    terminal = {'state': 'FAILED', 'result': None,
                                'error': {'code': 'GENERATION_FAILED', 'error_type': 'ConnectTimeout'}}
                    trace = None
                else:
                    terminal = {'state': 'SUCCEEDED', 'error': None, 'result': {
                        'result_type': 'facts', 'answer_status': 'answered',
                        'paragraphs': [{'text': 'local unit only', 'support_unit_ids': ['local-unit']}]}}
                    trace = {'answer_generated': True, 'completion': {'requested_state': 'SUCCEEDED', 'error': None},
                             'provider_calls': [{'purpose': 'generate'}]}
                observation = {'case_id': case['case_id'], 'request_id': 'local-' + case['case_id'],
                    'conversation_id': conversation, 'state': terminal['state'],
                    'continuation_gate': factual_gate(terminal, trace)}
                run.case_observations.append(observation)
                return observation

            run.ask = ask
            factory = Mock(return_value=run)
            with patch.dict(driver['_run_questions'].__globals__, {'Stage8Run': factory}):
                driver['_run_questions']('local-unit-plan-not-read', str(run.root))
            factory.assert_called_once_with('local-unit-plan-not-read', str(run.root))
            self.assertEqual(submitted, ['FIRST', 'INDEPENDENT'])
            self.assertEqual([item['case_id'] for item in run.case_observations], ['FIRST', 'FOLLOW', 'INDEPENDENT'])
            skipped = run.case_observations[1]
            self.assertEqual(skipped['state'], 'NOT_RUN_DEPENDENCY_FAILED')
            self.assertIsNone(skipped['request_id'])
            self.assertEqual(skipped['question_post_count'], 0)
            self.assertEqual(skipped['dependency']['case_id'], 'FIRST')
            self.assertEqual(skipped['dependency']['continuation_gate']['reason'], 'PREVIOUS_REQUEST_NOT_SUCCESSFUL')
            saved = json.loads((run.root / 'FOLLOW' / 'observation.json').read_bytes())
            self.assertEqual(saved, skipped)
            self.assertTrue(run.case_observations[2]['continuation_gate']['ready'])
            run.client.close.assert_called_once_with()
            print(json.dumps({'case': 'driver_dependency',
                'scope': 'LOCAL_DRIVER_LOGIC_UNIT_NO_REAL_MULTI_TURN_OR_RAGAS_CREDIT',
                'submitted_cases': submitted, 'followup_state': skipped['state'],
                'followup_question_post_count': skipped['question_post_count']}))


class MaintenanceBindingBoundaries(unittest.TestCase):
    def test_archive_total_size_and_file_identity_binding_are_separate(self):
        import hashlib
        from raghub.ingestion.archive import verify_archive
        from raghub.maintenance import update_file_bindings

        with tempfile.TemporaryDirectory(prefix='rg-f26-bind-', dir='/tmp') as scratch:
            root = Path(scratch).resolve(strict=True)
            original = root / 'original.md'
            original.write_bytes(b'A' * 477)
            original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
            manifest = root / 'archive-manifest.json'
            record = {'schema_version': 1, 'files': [
                {'path': 'original.md', 'sha256': original_sha, 'size_bytes': 477}]}
            manifest.write_bytes((json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
            manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.assertEqual(original.stat().st_size, 477)
            self.assertEqual(manifest.stat().st_size, 147)

            archive = verify_archive(manifest)
            self.assertEqual(archive['size_bytes'], 477)
            payload = {'archive': {**archive, 'manifest_sha256': '0' * 64}}
            mapped = update_file_bindings(payload, root)
            self.assertEqual(mapped['archive'], archive)
            self.assertEqual(payload['archive']['size_bytes'], 477)
            self.assertEqual(payload['archive']['manifest_sha256'], '0' * 64)

            identity = {'path': str(original), 'sha256': '0' * 64, 'size_bytes': 0,
                        'manifest_path': str(manifest), 'manifest_sha256': '0' * 64}
            for ordered in (identity, dict(reversed(tuple(identity.items())))):
                with self.subTest(field_order=tuple(ordered)):
                    rebound = update_file_bindings({'files': [ordered]}, root)['files'][0]
                    self.assertEqual(rebound['size_bytes'], 477)
                    self.assertEqual(rebound['sha256'], original_sha)
                    self.assertEqual(rebound['manifest_sha256'], manifest_sha)
                    self.assertEqual(ordered['size_bytes'], 0)
            print(json.dumps({'case': 'restored_archive_size_binding',
                'scope': 'LOCAL_MAINTENANCE_UNIT_NO_REAL_RECOVERY_OR_FORMAL_CREDIT',
                'original_size_bytes': 477, 'manifest_size_bytes': 147,
                'single_file_field_orders_checked': 2}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
