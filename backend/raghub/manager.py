"""Unified service start/status/stop with persistent launch intent and role locks."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import httpx
import psutil

from .config import Settings
from .process_identity import process_start_time
from .db import migrate, open_db, system_state
from .process_locks import ROLES, RoleLock, reserve_role, role_status

COMPOSE_PROJECT = "raghub"


def compose_project(settings: Settings) -> str:
    import hashlib
    if settings.home == (Path.home() / "MossHub"):
        return COMPOSE_PROJECT
    return "raghub-" + hashlib.sha256(str(settings.home).encode()).hexdigest()[:12]
COMPOSE_PHASES = {"STOPPED", "STARTING", "READY", "START_UNCONFIRMED", "STOPPING", "STOP_UNCONFIRMED"}


def docker_binary() -> Path:
    chosen = os.environ.get("RAG_DOCKER_BIN")
    options = [Path(chosen)] if chosen else [
        (Path.home() / "Applications/Docker.app/Contents/Resources/bin/docker"),
        Path("/Applications/Docker.app/Contents/Resources/bin/docker"),
        Path("/opt/homebrew/bin/docker"),
    ]
    for path in options:
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
    raise RuntimeError("Docker Desktop CLI is unavailable at a fixed absolute path")


@contextmanager
def manager_lock(settings: Settings) -> Iterator[None]:
    with (settings.home / "tmp/manager.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def child_env(settings: Settings) -> dict[str, str]:
    return {
        "RAG_HOME": str(settings.home),
        "HOME": str(settings.home),
        "TMPDIR": str(settings.home / "tmp"),
        "PYTHONPATH": str(settings.project / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CACHE_HOME": str(settings.home / "cache/xdg"),
        "PLAYWRIGHT_BROWSERS_PATH": str(settings.home / "runtime/browsers"),
        "HF_HOME": str(settings.home / "cache/huggingface"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "4",
        "LANG": "en_US.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }


def write_json(path: Path, value: dict) -> None:
    staged = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with staged.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    staged.replace(path)


class ComposeError(RuntimeError):
    def __init__(self, message: str, evidence_path: Path):
        super().__init__(f"{message}; raw child capture: {evidence_path}")
        self.evidence_path = str(evidence_path)


def compose(settings: Settings, *args: str, timeout: int = 900) -> dict:
    """Capture child pipes before interpreting an exit, including partial timeout bytes."""
    docker = docker_binary()
    env = {
        "RAG_HOME": str(settings.home),
        "HOME": str(Path.home()),
        "PATH": f"{docker.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if "DOCKER_CONFIG" in os.environ:
        env["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    argv = [str(docker), "compose", "-p", compose_project(settings), "-f", str(settings.compose), *args]
    capture_path = settings.logs / f"compose-{uuid4().hex}.json"
    began = time.time()
    stdout, stderr = b"", b""
    returncode, error = None, None
    started, timed_out, complete = False, False, False
    try:
        result = subprocess.run(
            argv, cwd=settings.project, env=env,
            capture_output=True, timeout=timeout, check=False,
        )
        started, complete = True, True
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        started, timed_out = True, True
        stdout, stderr = exc.output or b"", exc.stderr or b""
        error = type(exc).__name__
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    capture = {
        "schema": "raghub.compose-command.v1", "argv": argv,
        "cwd": str(settings.project), "passed_child_environment": env,
        "actual_child_environment": "NOT_OBSERVED",
        "started": started, "timed_out": timed_out, "returncode": returncode,
        "child_stream_split_observed": started, "child_pipe_capture_complete": complete,
        "child_stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "child_stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "started_at_unix": began, "ended_at_unix": time.time(), "method_error": error,
    }
    write_json(capture_path, capture)
    if not complete or returncode != 0:
        raise ComposeError(
            f"docker compose {' '.join(args)} failed: exit={returncode}, timed_out={timed_out}, error={error}",
            capture_path,
        )
    return {"child_stdout": stdout, "child_stderr": stderr, "evidence_path": str(capture_path)}


def observe_compose(settings: Settings) -> dict:
    """Query only the fixed Compose project; an unreadable query stays UNKNOWN."""
    try:
        result = compose(settings, "ps", "--all", "--format", "json", timeout=30)
    except Exception as exc:
        return {
            "status": "UNKNOWN", "error": str(exc),
            "evidence_path": getattr(exc, "evidence_path", None),
        }
    try:
        text = result["child_stdout"].decode("utf-8").strip()
        if not text:
            containers = []
        elif text.startswith("["):
            containers = json.loads(text)
        else:
            containers = [json.loads(line) for line in text.splitlines()]
        if not isinstance(containers, list) or any(not isinstance(item, dict) for item in containers):
            raise ValueError("compose ps did not return container objects")
    except (UnicodeError, ValueError) as exc:
        return {"status": "UNKNOWN", "error": str(exc), "evidence_path": result["evidence_path"]}
    return {"status": "OBSERVED", "containers": containers, "evidence_path": result["evidence_path"]}


def read_state(settings: Settings) -> dict:
    if not settings.run_state.exists():
        return {"services": {}, "compose": {"phase": "STOPPED"}}
    state = json.loads(settings.run_state.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("services"), dict):
        raise RuntimeError("invalid service state file")
    if any(name not in ROLES or not isinstance(item, dict) for name, item in state["services"].items()):
        raise RuntimeError("invalid recorded service roles")
    if "compose" not in state:
        # A legacy false flag is only a recorded value. start/stop still query Docker.
        state["compose"] = {"phase": "READY" if state.pop("compose_started", False) else "STOPPED"}
    if not isinstance(state["compose"], dict) or state["compose"].get("phase") not in COMPOSE_PHASES:
        raise RuntimeError("invalid recorded compose phase")
    return state


def save_state(settings: Settings, state: dict) -> None:
    write_json(settings.run_state, state)


def process_matches(record: dict) -> bool:
    try:
        process = psutil.Process(int(record["pid"]))
        return process.is_running() and abs(process_start_time(process) - record["created_at"]) < 0.01
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


def spawn(settings: Settings, state: dict, name: str, argv: list[str], lock: RoleLock) -> None:
    log_path = settings.logs / f"{name}.log"
    record = {
        "phase": "STARTING", "launch_id": lock.launch_id,
        "argv": argv, "log": str(log_path), "pid": None, "created_at": None,
    }
    state["services"][name] = record
    save_state(settings, state)
    env = child_env(settings)
    env.update(
        RAG_SERVICE_ROLE=name, RAG_SERVICE_LOCK_FD=str(lock.fd),
        RAG_SERVICE_LAUNCH_ID=lock.launch_id,
    )
    try:
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                argv, cwd=settings.project, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=(lock.fd,),
            )
        record["pid"] = process.pid
        try:
            record["created_at"] = process_start_time(psutil.Process(process.pid))
        except psutil.NoSuchProcess:
            record["created_at"] = None
        record["phase"] = "STARTED"
        save_state(settings, state)
    except Exception as exc:
        record["phase"] = "START_UNCONFIRMED"
        record["error"] = f"{type(exc).__name__}: {exc}"
        save_state(settings, state)
        raise
    finally:
        # The child inherited this same lock before the parent closes its copy.
        lock.close()


def wait_ready(url: str, record: dict, *, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    last = "not yet responding"
    while time.monotonic() < deadline:
        if not process_matches(record):
            raise RuntimeError(f"service exited before ready: {record['log']}")
        try:
            response = httpx.get(url, timeout=3, trust_env=False)
            data = response.json()
            if response.status_code == 200 and isinstance(data, dict) and data.get("ready") is True:
                return
            last = f"HTTP {response.status_code}: {str(data)[:300]}"
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise RuntimeError(f"service did not become ready: {url}: {last}")


def wait_workers_ready(state: dict, *, seconds: int = 90) -> None:
    from .worker_app import app as celery_app

    expected = {"document-worker": "document", "ocr-worker": "ocr"}
    deadline = time.monotonic() + seconds
    last = "control channel not responding"
    while time.monotonic() < deadline:
        if any(not process_matches(state["services"][name]) for name in expected):
            raise RuntimeError("worker exited before queue readiness")
        inspector = celery_app.control.inspect(
            destination=[f"{name}@raghub" for name in expected], timeout=3
        )
        active = inspector.active_queues() or {}
        if all(
            expected[name] in {item["name"] for item in active.get(f"{name}@raghub", [])}
            for name in expected
        ):
            return
        last = f"active queues: {active}"
        time.sleep(2)
    raise RuntimeError(f"workers did not subscribe to required queues: {last}")


def require_loopback_ports_free() -> None:
    for port in (8000, 8001, 19530, 6379):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                # Match the TCP server rebind semantics without permitting another listener.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"required loopback port is unavailable: {port}") from exc


def start(settings: Settings) -> dict:
    settings.check_business_start()
    settings.check_layout()
    if Path(sys.executable) != settings.python:
        raise RuntimeError(f"entry interpreter must be {settings.python}")
    with manager_lock(settings), ExitStack() as roles:
        state = read_state(settings)
        if state["services"] or state["compose"]["phase"] != "STOPPED":
            raise RuntimeError("recorded launch state exists; inspect and stop before restart")
        # Reserve all roles before external startup, including the Popen/register window.
        locks = {name: roles.enter_context(reserve_role(settings, name)) for name in ROLES}
        observed = observe_compose(settings)
        if observed["status"] != "OBSERVED" or observed["containers"]:
            raise RuntimeError("Compose project is occupied or unobservable; no adoption")
        require_loopback_ports_free()
        from .schema import maintenance_upgrade
        with closing(open_db(settings.sqlite, create=True)) as db:
            maintenance_upgrade(db, settings.project / "backend/migrations", settings.logs)
            system_state(db)
        state["compose"] = {"phase": "STARTING", "intent": ["up", "-d", "--wait"]}
        save_state(settings, state)
        try:
            outcome = compose(settings, "up", "-d", "--wait")
        except Exception as exc:
            state["compose"].update(
                phase="START_UNCONFIRMED", error=str(exc),
                failure_evidence=getattr(exc, "evidence_path", None),
                observed_after_failure=observe_compose(settings),
            )
            save_state(settings, state)
            raise
        state["compose"].update(phase="READY", last_evidence=outcome["evidence_path"])
        save_state(settings, state)
        python = str(settings.python)
        spawn(
            settings, state, "model",
            [python, "-B", "-m", "uvicorn", "raghub.model_service:app", "--host", "127.0.0.1",
             "--port", "8001", "--no-access-log"], locks["model"],
        )
        wait_ready("http://127.0.0.1:8001/health/ready", state["services"]["model"], seconds=360)
        spawn(
            settings, state, "api",
            [python, "-B", "-m", "uvicorn", "raghub.api:app", "--host", "127.0.0.1",
             "--port", "8000", "--no-access-log"], locks["api"],
        )
        wait_ready("http://127.0.0.1:8000/health/ready", state["services"]["api"], seconds=90)
        for name, queue in (("document-worker", "document"), ("ocr-worker", "ocr")):
            spawn(
                settings, state, name,
                [python, "-B", "-m", "celery", "-A", "raghub.worker_app:app", "worker",
                 "--pool=solo", "--concurrency=1", "--queues", queue,
                 "--hostname", f"{name}@raghub", "--loglevel=WARNING"], locks[name],
            )
        wait_workers_ready(state)
        spawn(settings, state, "dispatcher", [python, "-B", "-m", "raghub.dispatcher"], locks["dispatcher"])
        time.sleep(2)
        if not process_matches(state["services"]["dispatcher"]):
            raise RuntimeError("dispatcher exited before ready")
        return status(settings, state)


def owned_process(name: str, record: dict | None, observed: dict) -> dict | None:
    if observed["held"]:
        owner = observed["owner"]
        if record is None or owner is None or owner.get("launch_id") != record.get("launch_id"):
            raise RuntimeError(f"{name} role is occupied without a matching launch identity; no adoption")
        if not process_matches(owner):
            raise RuntimeError(f"{name} role holder identity is unresolved; state retained")
        if process_matches(record) and (owner["pid"], owner["created_at"]) != (record["pid"], record["created_at"]):
            raise RuntimeError(f"{name} process and role holder disagree; state retained")
        return owner
    return record if record is not None and process_matches(record) else None


def status(settings: Settings, state: dict | None = None) -> dict:
    if state is None:
        state = read_state(settings)
    services = {}
    for name in ROLES:
        record = state["services"].get(name)
        observed = role_status(settings, name)
        services[name] = {"recorded": record, "observed_role_lock": observed}
        try:
            process = owned_process(name, record, observed)
            services[name].update(running=process is not None, pid=process["pid"] if process else None)
        except RuntimeError as exc:
            services[name].update(running="UNKNOWN", error=str(exc))
    return {"compose": {"recorded": state["compose"], "observed": observe_compose(settings)}, "services": services}


def stop(settings: Settings) -> dict:
    with manager_lock(settings):
        state = read_state(settings)
        for name in ("dispatcher", "api", "ocr-worker", "document-worker", "model"):
            record = state["services"].get(name)
            process_record = owned_process(name, record, role_status(settings, name))
            if process_record is not None:
                try:
                    process = psutil.Process(process_record["pid"])
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=60)
                except psutil.NoSuchProcess:
                    pass
                except psutil.TimeoutExpired as exc:
                    raise RuntimeError(f"{name} did not stop within 60 seconds; state retained") from exc
            if role_status(settings, name)["held"]:
                raise RuntimeError(f"{name} role is still occupied; state retained")
            if record is not None:
                del state["services"][name]
                save_state(settings, state)
        if state["compose"]["phase"] != "STOPPED":
            state["compose"]["phase"] = "STOPPING"
            save_state(settings, state)
            try:
                outcome = compose(settings, "down", timeout=120)
            except Exception as exc:
                state["compose"].update(
                    phase="STOP_UNCONFIRMED", error=str(exc),
                    failure_evidence=getattr(exc, "evidence_path", None),
                    observed_after_failure=observe_compose(settings),
                )
                save_state(settings, state)
                raise
            observed = observe_compose(settings)
            state["compose"].update(last_evidence=outcome["evidence_path"], observed_after_stop=observed)
            if observed["status"] != "OBSERVED" or observed["containers"]:
                state["compose"]["phase"] = "STOP_UNCONFIRMED"
                save_state(settings, state)
                raise RuntimeError("Compose stop is unconfirmed; launch state retained")
            state["compose"]["phase"] = "STOPPED"
            save_state(settings, state)
        else:
            observed = observe_compose(settings)
            if observed["status"] != "OBSERVED" or observed["containers"]:
                raise RuntimeError("Compose project is occupied or unobservable without launch intent; no adoption")
        return status(settings, state)


def upgrade(settings: Settings) -> dict:
    """Exclusive maintenance entry; stop all services before calling."""
    settings.check_layout()
    with manager_lock(settings), ExitStack() as roles:
        state = read_state(settings)
        if state["services"] or state["compose"]["phase"] != "STOPPED":
            raise RuntimeError("stop services before upgrading SQLite")
        for name in ROLES:
            roles.enter_context(reserve_role(settings, name))
        from .schema import maintenance_upgrade
        with closing(open_db(settings.sqlite, create=True)) as db:
            has_tasks = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", "tasks")
            ).fetchone()
            if has_tasks and db.execute(
                "SELECT 1 FROM tasks WHERE status IN (?,?) LIMIT 1", ("PENDING", "RUNNING")
            ).fetchone():
                raise RuntimeError("active tasks must stop before upgrading SQLite")
            return maintenance_upgrade(db, settings.project / "backend/migrations", settings.logs)


def restart_api(settings: Settings) -> dict:
    """Reload only the API after ordinary source/bundle changes."""
    settings.check_business_start()
    with manager_lock(settings):
        with closing(open_db(settings.sqlite)) as db:
            if db.execute("SELECT 1 FROM retrieval_requests WHERE state IN (?,?) LIMIT 1",
                          ("RUNNING","CANCELLING")).fetchone():
                raise RuntimeError("active questions must finish before reloading API")
        state=read_state(settings)
        record=state["services"].get("api")
        owned=owned_process("api",record,role_status(settings,"api"))
        if owned:
            process=psutil.Process(owned["pid"]);process.terminate();process.wait(timeout=60)
        if role_status(settings,"api")["held"]:raise RuntimeError("API role still occupied")
        state["services"].pop("api",None);save_state(settings,state)
        with reserve_role(settings,"api") as lock:
            spawn(settings,state,"api",[str(settings.python),"-B","-m","uvicorn","raghub.api:app",
                                      "--host",settings.api_host,"--port",str(settings.api_port),"--no-access-log"],lock)
        wait_ready("http://"+settings.api_host+":"+str(settings.api_port)+"/health/ready",state["services"]["api"],seconds=90)
        return {"api":"RESTARTED","pid":state["services"]["api"]["pid"],"other_services_changed":False}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop", "upgrade", "restart-api"])
    args = parser.parse_args()
    settings = Settings.load()
    try:
        if args.action == "restart-api":
            result = restart_api(settings)
        elif args.action == "upgrade":
            result = upgrade(settings)
        elif args.action == "start":
            result = start(settings)
        elif args.action == "stop":
            result = stop(settings)
        else:
            result = status(settings)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
