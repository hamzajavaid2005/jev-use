"""Private, resumable checkpoints for a single Facebook check-in flow.

Tokens are opaque UUIDs and resolve only beneath the server-owned
``facebook-flows`` directory. The account, place, audience, and device are fixed
when the checkpoint is created. Call :func:`save` with each next stage before
performing its action; in particular, persist ``submitting`` before tapping the
publish control so a resumed flow cannot publish twice automatically.

This module stores workflow metadata only. It has no field for passwords or
other credentials.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import host


STAGES = ("created", "logging_in", "identity_verified", "location_checked",
          "composing", "pre-submit", "submitting", "posted", "logging_out",
          "logged_out")
_STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
_TOKEN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_FIELDS = frozenset({"token", "serial", "account", "place", "audience", "stage",
                     "evidence", "created", "updated"})
_SECRET_KEY_PARTS = ("password", "passwd", "credential", "secret", "auth", "cookie",
                     "key", "token")
_LOCK = threading.RLock()


def _state_dir() -> Path:
    return host.work_root() / "facebook-flows"


def _path(token: str) -> Path:
    validate_token_format(token)
    return _state_dir() / f"{token}.json"


def validate_token_format(token: str) -> None:
    """Reject malformed handles before filesystem access."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("flow token is invalid")


def _validate_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _validate_state(value: Any, token: str) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or value.get("token") != token
            or value.get("stage") not in _STAGE_INDEX):
        raise ValueError("flow checkpoint failed validation")
    for field in ("serial", "account", "place", "audience"):
        _validate_text(field, value.get(field))
    _validate_evidence(value.get("evidence"))
    for field in ("created", "updated"):
        stamp = value.get(field)
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise ValueError("flow checkpoint failed validation")
    if value["updated"] < value["created"]:
        raise ValueError("flow checkpoint failed validation")
    return value


def _validate_evidence(evidence: Any) -> None:
    """Accept only a small map of non-secret scalar observations."""
    if not isinstance(evidence, dict) or len(evidence) > 32:
        raise ValueError("flow evidence must be a small object")
    for key, value in evidence.items():
        if (not isinstance(key, str) or not key or len(key) > 100
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", key)
                or any(part in re.sub(r"[^a-z0-9]", "", key.casefold())
                       for part in _SECRET_KEY_PARTS)):
            raise ValueError("flow evidence contains an invalid or secret-like key")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("flow evidence values must be scalar")
        if isinstance(value, str) and len(value) > 2048:
            raise ValueError("flow evidence string is too long")
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("flow evidence number must be finite")


def _read(token: str) -> dict[str, Any]:
    path = _path(token)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("flow token was not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("flow checkpoint is unavailable or damaged") from exc
    return _validate_state(value, token)


def _write(token: str, value: dict[str, Any]) -> None:
    value = _validate_state(value, token)
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(token)
    fd, temporary = tempfile.mkstemp(prefix=".flow-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create(*, serial: str, account: str, place: str, audience: str,
           run_id: str | None = None) -> dict[str, Any]:
    """Create a flow checkpoint at ``created`` and return its state/token."""
    for name, value in (("serial", serial), ("account", account),
                        ("place", place), ("audience", audience)):
        _validate_text(name, value)
    with _LOCK:
        now = time.time()
        if run_id is not None:
            _validate_text("run_id", run_id)
            if len(run_id) > 200:
                raise ValueError("run_id is too long")
            token = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([serial, account, run_id])))
            if _path(token).exists():
                state = _read(token)
                if any(state[key] != value for key, value in
                       (("serial", serial), ("account", account), ("place", place), ("audience", audience))):
                    raise ValueError("run_id is already bound to different flow parameters")
                return dict(state)
        else:
            token = str(uuid.uuid4())
        state = {"token": token, "serial": serial, "account": account,
                 "place": place, "audience": audience, "stage": "created",
                 "evidence": {},
                 "created": now, "updated": now}
        _write(token, state)
        return dict(state)


def load(token: str) -> dict[str, Any]:
    """Load and validate a checkpoint using its opaque token."""
    with _LOCK:
        return dict(_read(token))


def save(token: str, stage: str, *, expected_stage: str | None = None,
         evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Advance exactly one stage and atomically persist it.

    ``expected_stage`` provides a simple stale-writer guard. Callers must save
    Advance through :data:`STAGES` in order. Save ``submitting`` before the
    publish tap, then inspect the UI before saving ``posted``. Optional evidence
    is merged into the journal after scalar/secret-key validation. No skipped or
    repeated stages are accepted.
    """
    if not isinstance(stage, str) or stage not in _STAGE_INDEX:
        raise ValueError("stage is invalid")
    with _LOCK:
        state = _read(token)
        current = state["stage"]
        if expected_stage is not None and expected_stage != current:
            raise ValueError("flow checkpoint stage changed")
        if _STAGE_INDEX[stage] != _STAGE_INDEX[current] + 1:
            raise ValueError("flow checkpoint must advance exactly one stage")
        if evidence is not None:
            _validate_evidence(evidence)
            merged = {**state["evidence"], **evidence}
            _validate_evidence(merged)
            state["evidence"] = merged
        state["stage"] = stage
        state["updated"] = max(time.time(), state["created"])
        _write(token, state)
        return dict(state)
