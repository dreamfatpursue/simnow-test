"""Single-process activity gate shared by confirmed trading entrypoints."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO


@dataclass(frozen=True)
class ActivityIdentity:
    """Credential-free facts needed to find the current local run."""

    run_id: str
    pid: int
    started_at: str
    environment: str
    market_data_mode: str
    strategy_hash: str
    contracts: tuple[str, ...]
    audit_dir: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "environment": self.environment,
            "market_data_mode": self.market_data_mode,
            "strategy_hash": self.strategy_hash,
            "contracts": list(self.contracts),
            "audit_dir": self.audit_dir,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ActivityIdentity":
        return cls(
            run_id=str(value["run_id"]),
            pid=int(value["pid"]),
            started_at=str(value["started_at"]),
            environment=str(value["environment"]),
            market_data_mode=str(value["market_data_mode"]),
            strategy_hash=str(value["strategy_hash"]),
            contracts=tuple(str(item) for item in value["contracts"]),
            audit_dir=str(value["audit_dir"]),
        )

    def with_audit_dir(self, audit_dir: str | Path) -> "ActivityIdentity":
        return replace(self, audit_dir=str(audit_dir))


def default_activity_lock_path(project_root: Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[1]
    return root / "audit" / ".active-run.lock"


def _read_identity(handle: TextIO) -> ActivityIdentity | None:
    handle.seek(0)
    try:
        value = json.load(handle)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        return ActivityIdentity.from_dict(value)
    except (KeyError, TypeError, ValueError):
        return None


class ActivityLock:
    """An advisory POSIX lock held for the lifetime of one trading process."""

    def __init__(self, path: Path, handle: TextIO, identity: ActivityIdentity) -> None:
        self.path = path
        self.identity = identity
        self._handle: TextIO | None = handle

    @classmethod
    def try_acquire(
        cls,
        path: str | Path,
        identity: ActivityIdentity,
    ) -> tuple["ActivityLock | None", ActivityIdentity | None]:
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                handle.close()
                raise
            current = _read_identity(handle)
            handle.close()
            return None, current

        lock = cls(lock_path, handle, identity)
        try:
            lock.update_identity(identity)
        except Exception:
            lock.release()
            raise
        return lock, None

    @classmethod
    def read_active(cls, path: str | Path) -> ActivityIdentity | None:
        """Return the identity only while another process holds the lock."""
        lock_path = Path(path)
        try:
            handle = lock_path.open("a+", encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                handle.close()
                raise
            current = _read_identity(handle)
            handle.close()
            return current
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            return None

    @classmethod
    def read_record(cls, path: str | Path) -> ActivityIdentity | None:
        """Read the last credential-free identity without treating it as active."""
        lock_path = Path(path)
        try:
            handle = lock_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            return _read_identity(handle)
        finally:
            handle.close()

    def update_identity(self, identity: ActivityIdentity) -> None:
        handle = self._handle
        if handle is None:
            raise RuntimeError("活动运行锁已经释放")
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(identity.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self.identity = identity

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "ActivityLock":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
