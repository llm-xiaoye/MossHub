"""Process-lifetime role locks shared by the manager and service entries."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import psutil

from .config import Settings
from .process_identity import process_start_time

ROLES = ("model", "api", "document-worker", "ocr-worker", "dispatcher")


def role_paths(settings: Settings, role: str) -> tuple[Path, Path]:
    if role not in ROLES:
        raise ValueError(f"unknown service role: {role}")
    return (settings.home / f"tmp/role-{role}.lock", settings.home / f"tmp/role-{role}.json")


class RoleLock:
    def __init__(self, settings: Settings, role: str, fd: int, launch_id: str):
        self.settings, self.role, self.fd, self.launch_id = settings, role, fd, launch_id

    def close(self) -> None:
        if self.fd is not None:
            # An inherited descriptor shares the flock's open-file description.
            # LOCK_UN here would also unlock the still-running child.
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "RoleLock":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def reserve_role(settings: Settings, role: str) -> RoleLock:
    path, _owner_path = role_paths(settings, role)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"service role is occupied: {role}") from exc
    return RoleLock(settings, role, fd, str(uuid4()))


def enter_service_role(settings: Settings, role: str) -> RoleLock:
    """Reuse the manager's reserved lock, or acquire one for a direct entry."""
    settings.check_business_start()
    names = ("RAG_SERVICE_ROLE", "RAG_SERVICE_LOCK_FD", "RAG_SERVICE_LAUNCH_ID")
    values = [os.environ.get(name) for name in names]
    if any(value is not None for value in values):
        if not all(values) or values[0] != role:
            raise RuntimeError("incomplete or mismatched inherited role lock")
        fd = int(values[1])
        path, _owner_path = role_paths(settings, role)
        opened, actual = os.fstat(fd), path.stat()
        if (opened.st_dev, opened.st_ino) != (actual.st_dev, actual.st_ino):
            raise RuntimeError("inherited role lock does not match its file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = RoleLock(settings, role, fd, values[2])
    else:
        lock = reserve_role(settings, role)
    try:
        os.set_inheritable(lock.fd, False)
        _path, owner_path = role_paths(settings, role)
        owner = {
            "role": role, "launch_id": lock.launch_id,
            "pid": os.getpid(), "created_at": process_start_time(psutil.Process()),
        }
        staged = owner_path.with_name(owner_path.name + "." + uuid4().hex + ".tmp")
        with staged.open("x", encoding="utf-8") as handle:
            json.dump(owner, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(owner_path)
    except BaseException:
        lock.close()
        raise
    return lock


@contextmanager
def service_role(settings: Settings, role: str) -> Iterator[RoleLock]:
    with enter_service_role(settings, role) as lock:
        yield lock


def role_status(settings: Settings, role: str) -> dict:
    path, owner_path = role_paths(settings, role)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return {"held": False, "owner": None}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
    finally:
        os.close(fd)
    owner, error = None, None
    if held:
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if not isinstance(owner, dict) or owner.get("role") != role:
                raise ValueError("invalid role owner record")
        except (OSError, ValueError) as exc:
            owner, error = None, type(exc).__name__
    return {"held": held, "owner": owner, "owner_error": error}
