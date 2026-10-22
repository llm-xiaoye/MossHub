"""Local lifecycle regressions; these checks do not run F01 or containers."""

from __future__ import annotations

import base64
import json
import select
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from celery.signals import worker_init, worker_shutdown

from raghub import manager, worker_app
from raghub.config import Settings
from raghub.db import migrate, open_db
from raghub.process_locks import reserve_role, role_status


@contextmanager
def isolated_settings():
    with tempfile.TemporaryDirectory(prefix="rag-s1-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        settings = replace(
            Settings.load(), home=root, python=Path(sys.executable),
            sqlite=root / "data/sqlite/rag.sqlite3", logs=root / "logs",
            run_state=root / "tmp/services.json",
        )
        for path in settings.required_dirs():
            path.mkdir(parents=True, exist_ok=True)
        with closing(open_db(settings.sqlite, create=True)) as db:
            migrate(db, settings.project / "backend/migrations")
        yield settings


class ComposeLifecycleTest(unittest.TestCase):
    def test_partial_up_and_timeout_keep_intent_for_stop(self) -> None:
        for failure in ("nonzero", "timeout"):
            with self.subTest(failure=failure), isolated_settings() as settings:
                partial = False
                invocations = []

                def run(argv, **_kwargs):
                    nonlocal partial
                    invocations.append(argv)
                    self.assertEqual(argv[1:4], ["compose", "-p", manager.compose_project(settings)])
                    if "up" in argv:
                        self.assertEqual(manager.read_state(settings)["compose"]["phase"], "STARTING")
                        partial = True
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(argv, 5, output=b"created etcd\n", stderr=b"not ready\n")
                        return subprocess.CompletedProcess(argv, 17, b"created etcd\n", b"minio failed\n")
                    if "down" in argv:
                        self.assertEqual(manager.read_state(settings)["compose"]["phase"], "STOPPING")
                        self.assertNotIn("--volumes", argv)
                        partial = False
                        return subprocess.CompletedProcess(argv, 0, b"", b"removed project containers\n")
                    if "ps" in argv:
                        objects = [{"Name": "raghub-etcd-1", "State": "running"}] if partial else []
                        return subprocess.CompletedProcess(argv, 0, json.dumps(objects).encode() + b"\n", b"")
                    self.fail(f"unexpected unit-test invocation: {argv}")

                with patch.object(manager, "docker_binary", return_value=Path("/fixed/docker")), \
                     patch.object(manager.subprocess, "run", side_effect=run), \
                     patch.object(manager, "require_loopback_ports_free"):
                    with self.assertRaises(manager.ComposeError):
                        manager.start(settings)
                    failed = manager.read_state(settings)
                    self.assertEqual(failed["compose"]["phase"], "START_UNCONFIRMED")
                    self.assertEqual(len(failed["compose"]["observed_after_failure"]["containers"]), 1)
                    raw = json.loads(Path(failed["compose"]["failure_evidence"]).read_text())
                    self.assertEqual(base64.b64decode(raw["child_stdout_base64"]), b"created etcd\n")
                    self.assertEqual(raw["timed_out"], failure == "timeout")
                    self.assertEqual(raw["child_pipe_capture_complete"], failure != "timeout")
                    stopped = manager.stop(settings)
                    self.assertEqual(stopped["compose"]["recorded"]["phase"], "STOPPED")
                    self.assertEqual(stopped["compose"]["observed"]["containers"], [])
                self.assertEqual(sum("up" in argv for argv in invocations), 1)
                self.assertEqual(sum("down" in argv for argv in invocations), 1)


class ProcessRoleTest(unittest.TestCase):
    def test_child_holds_lock_and_stop_resolves_registration_gap(self) -> None:
        with isolated_settings() as settings:
            lock = reserve_role(settings, "dispatcher")
            state = manager.read_state(settings)
            state["services"]["dispatcher"] = {
                "phase": "STARTING", "launch_id": lock.launch_id, "pid": None, "created_at": None,
            }
            manager.save_state(settings, state)
            env = manager.child_env(settings)
            env.update(
                RAG_SERVICE_ROLE="dispatcher", RAG_SERVICE_LOCK_FD=str(lock.fd),
                RAG_SERVICE_LAUNCH_ID=lock.launch_id,
            )
            source = (
                "import sys\n"
                "from raghub.config import Settings\n"
                "from raghub.process_locks import service_role\n"
                "with service_role(Settings.load(), 'dispatcher'):\n"
                " print('LOCKED', flush=True)\n"
                " sys.stdin.readline()\n"
            )
            child = None
            try:
                child = subprocess.Popen(
                    [str(settings.python), "-B", "-c", source], cwd=settings.project, env=env,
                    pass_fds=(lock.fd,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "lock child did not report")
                self.assertEqual(child.stdout.readline(), b"LOCKED\n")
                lock.close()
                observed = role_status(settings, "dispatcher")
                self.assertTrue(observed["held"])
                self.assertEqual(manager.owned_process("dispatcher", state["services"]["dispatcher"], observed)["pid"], child.pid)
                with self.assertRaises(RuntimeError):
                    reserve_role(settings, "dispatcher")
                with patch.object(manager, "observe_compose", return_value={
                    "status": "OBSERVED", "containers": [], "evidence_path": "unit-test-no-docker-call",
                }):
                    manager.stop(settings)
                child.communicate(timeout=5)
                self.assertFalse(role_status(settings, "dispatcher")["held"])
                self.assertEqual(manager.read_state(settings)["services"], {})
            finally:
                lock.close()
                if child is not None:
                    if child.poll() is None:
                        child.terminate()
                    child.communicate(timeout=5)

    def test_worker_signal_rejects_duplicate_before_broker(self) -> None:
        class Worker:
            def __init__(self, role):
                self.hostname = f"{role}@raghub"

        with isolated_settings() as settings, patch.object(worker_app, "settings", settings):
            for role in ("document-worker", "ocr-worker"):
                with self.subTest(role=role):
                    with reserve_role(settings, role):
                        with self.assertRaises(SystemExit):
                            worker_init.send(sender=Worker(role))
                    worker_init.send(sender=Worker(role))
                    try:
                        self.assertTrue(role_status(settings, role)["held"])
                        with self.assertRaises(RuntimeError):
                            reserve_role(settings, role)
                    finally:
                        worker_shutdown.send(sender=Worker(role))
                    self.assertFalse(role_status(settings, role)["held"])

    def test_early_ack_and_broker_limits_are_explicit(self) -> None:
        config = worker_app.app.conf
        self.assertFalse(config.task_acks_late)
        self.assertFalse(config.task_reject_on_worker_lost)
        self.assertFalse(config.task_publish_retry)
        self.assertEqual(config.worker_prefetch_multiplier, 1)
        self.assertEqual(config.broker_transport_options["visibility_timeout"], 3600)
        self.assertEqual(config.visibility_timeout, 3600)


if __name__ == "__main__":
    unittest.main()
