"""Chunked, resumable Facebook location audits.

Resume handles are opaque UUIDs. They address files only below this module's
fixed state directory; callers never supply a path or account list.
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
from typing import Any, Callable

from . import host


DEFAULT_CHUNK_SIZE = 2
MAX_CHUNK_SIZE = 5
CHUNK_BUDGET_SECONDS = 52
DEFAULT_ACCOUNT_TIMEOUT = 50
_TOKEN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_LOCK = threading.RLock()


def _verified(record: dict[str, Any]) -> bool:
    location = record.get("location")
    return (record.get("state") == "location"
            and record.get("identity_verified") is True
            and isinstance(location, str) and bool(location.strip()))


def _state_dir() -> Path:
    # The location is server-owned and never derived from request arguments.
    return host.work_root() / "facebook-audits"


def _path(token: str) -> Path:
    validate_resume_token_format(token)
    return _state_dir() / f"{token}.json"


def validate_resume_token_format(token: str) -> None:
    """Reject malformed handles before device discovery or filesystem access."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("resume_token is invalid")


def validate_resume_token(token: str) -> None:
    """Validate that the opaque handle resolves to a valid server-owned state."""
    _read(token)


def _read(token: str) -> dict[str, Any]:
    path = _path(token)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("resume_token was not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("resume state is unavailable or damaged") from exc
    if not isinstance(value, dict) or value.get("token") != token:
        raise ValueError("resume state failed validation")
    accounts = value.get("accounts")
    results = value.get("results")
    index = value.get("next_index")
    if (not isinstance(accounts, list) or not accounts
            or any(not isinstance(name, str) or not name.strip() for name in accounts)
            or len({" ".join(name.split()).casefold() for name in accounts}) != len(accounts)
            or not isinstance(results, list) or isinstance(index, bool)
            or not isinstance(index, int) or not 0 <= index <= len(accounts)
            or len(results) != index
            or any(not isinstance(record, dict) or record.get("account") != accounts[i]
                   or not isinstance(record.get("state"), str)
                   or (record.get("location") is not None and
                       (record.get("state") != "location" or record.get("identity_verified") is not True))
                   for i, record in enumerate(results))):
        raise ValueError("resume state failed validation")
    inflight = value.get("in_flight")
    if inflight is not None and (not isinstance(inflight, dict)
            or inflight.get("index") != index or not 0 <= index < len(accounts)
            or inflight.get("account") != accounts[index]):
        raise ValueError("resume state failed validation")
    retry_inflight = value.get("retry_in_flight")
    if retry_inflight is not None and (not isinstance(retry_inflight, dict)
            or retry_inflight.get("account") not in accounts
            or isinstance(retry_inflight.get("index"), bool)
            or not isinstance(retry_inflight.get("index"), int)
            or not 0 <= retry_inflight["index"] < len(accounts)
            or retry_inflight.get("account") != accounts[retry_inflight["index"]]
            or not isinstance(retry_inflight.get("interrupted"), bool)):
        raise ValueError("resume state failed validation")
    return value


def _write(token: str, value: dict[str, Any]) -> None:
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(token)
    fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=directory)
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


def run(serial: str, *, timeout: float, chunk_size: int,
        resume_token: str | None,
        facebook_run: Callable[..., dict[str, Any]],
        chunk_budget_seconds: float = CHUNK_BUDGET_SECONDS,
        continue_after_blocker: bool = False,
        retry_current: bool = False) -> dict[str, Any]:
    """Run one bounded chunk. Device operations are intentionally sequential."""
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("device serial is required")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(f"chunk_size must be an integer from 1 to {MAX_CHUNK_SIZE}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ValueError("timeout must be a number from 1 to 60 seconds")
    if isinstance(chunk_budget_seconds, bool) or not isinstance(chunk_budget_seconds, (int, float)) or not 10 <= chunk_budget_seconds <= 55:
        raise ValueError("chunk_budget_seconds must be a number from 10 to 55 seconds")
    if timeout > chunk_budget_seconds:
        raise ValueError("timeout must not exceed chunk_budget_seconds")
    if not isinstance(continue_after_blocker, bool):
        raise ValueError("continue_after_blocker must be a boolean")
    if not isinstance(retry_current, bool):
        raise ValueError("retry_current must be a boolean")
    if continue_after_blocker and retry_current:
        raise ValueError("continue_after_blocker and retry_current are mutually exclusive")
    if retry_current and resume_token is None:
        raise ValueError("retry_current requires a resume_token")

    with _LOCK:
        call_started = time.monotonic()
        retry_target: dict[str, Any] | None = None
        if resume_token is None:
            listed = facebook_run(serial, action="accounts", timeout=timeout)
            if listed.get("state") != "accounts":
                return {
                    "device": serial, "state": listed.get("state", "blocked"),
                    "accounts": [], "results": [], "next_index": 0,
                    "complete": False, "blocker": listed,
                    "timings": {"call_seconds": listed.get("seconds", 0.0),
                                "enumeration_seconds": listed.get("seconds", 0.0),
                                "chunk_seconds": listed.get("seconds", 0.0), "processed_this_call": 0},
                }
            accounts = listed.get("accounts")
            if (not isinstance(accounts, list) or not accounts
                    or any(not isinstance(name, str) or not name.strip() for name in accounts)
                    or len({" ".join(name.split()).casefold() for name in accounts}) != len(accounts)):
                return {"device": serial, "state": "account_ambiguous", "accounts": [],
                        "results": [], "complete": False,
                        "detail": "Enumeration did not return a nonempty unique exact account list.",
                        "timings": {"call_seconds": listed.get("seconds", 0.0),
                                    "enumeration_seconds": listed.get("seconds", 0.0),
                                    "chunk_seconds": listed.get("seconds", 0.0), "processed_this_call": 0}}
            token = str(uuid.uuid4())
            state: dict[str, Any] = {"token": token, "device": serial,
                                     "accounts": accounts, "next_index": 0,
                                     "results": [], "enumeration_seconds": listed.get("seconds", 0.0),
                                     "created": time.time()}
            _write(token, state)
        else:
            token = resume_token
            state = _read(token)
            if state.get("device") != serial:
                raise ValueError("resume_token belongs to a different device")
            if (not isinstance(state.get("accounts"), list)
                    or not isinstance(state.get("results"), list)
                    or not isinstance(state.get("next_index"), int)):
                raise ValueError("resume state failed validation")
            if continue_after_blocker:
                readiness_started = time.monotonic()
                try:
                    ready = facebook_run(serial, action="ready", timeout=timeout)
                except Exception as exc:
                    ready = {"state": "device_error", "detail": str(exc)}
                if ready.get("state") != "ready":
                    return _session_blocked_response(state, token, ready,
                                                     time.monotonic() - readiness_started)
            if retry_current:
                if state.get("halted") == "device_or_session_blocker":
                    raise ValueError("retry_current cannot retry an account blocked before selection; use continue_after_blocker after the phone is ready")
                if not (state.get("halted") or state.get("in_flight") or state.get("retry_in_flight")):
                    raise ValueError("retry_current requires a halted or interrupted account")
                retry_marker = state.get("retry_in_flight")
                if retry_marker:
                    retry_index = retry_marker.get("index")
                    retry_account = retry_marker.get("account")
                    interrupted = bool(retry_marker.get("interrupted"))
                elif state.get("in_flight"):
                    retry_index = state["in_flight"].get("index")
                    retry_account = state["in_flight"].get("account")
                    interrupted = True
                else:
                    retry_index = state["next_index"] - 1
                    interrupted = False
                    if retry_index < 0:
                        raise ValueError("resume state has no failed account to retry")
                    retry_account = state["accounts"][retry_index]
                if (isinstance(retry_index, bool) or not isinstance(retry_index, int)
                        or not 0 <= retry_index < len(state["accounts"])
                        or retry_account != state["accounts"][retry_index]
                        or (not interrupted and retry_index >= len(state["results"]))):
                    raise ValueError("resume state failed validation")
                retry_target = {"index": retry_index, "account": retry_account,
                                "interrupted": interrupted}
            elif state.get("retry_in_flight") and not continue_after_blocker:
                return _blocked_response(state, token)
            elif state.get("halted") and not continue_after_blocker:
                return _blocked_response(state, token)
            if not retry_current and state.get("halted") and continue_after_blocker:
                state["halted"] = None
                state["last_blocker"] = None
                state["last_session_blocker"] = None
                state["retry_in_flight"] = None
            if not retry_current and state.get("in_flight") and not continue_after_blocker:
                # A process can die after sending a selection but before it
                # records the outcome. Never replay that potentially completed
                # selection automatically.
                state["halted"] = "interrupted_account_requires_inspection"
                _write(token, state)
                return _blocked_response(state, token)
            if not retry_current and state.get("in_flight") and continue_after_blocker:
                uncertain = state["in_flight"]
                account = uncertain.get("account")
                if not isinstance(account, str) or not 0 <= uncertain.get("index", -1) < len(state["accounts"]):
                    raise ValueError("resume state failed validation")
                state["results"].append({"account": account, "location": None,
                                         "identity_verified": False,
                                         "state": "interrupted_uncertain",
                                         "elapsed_seconds": None,
                                         "detail": "Interrupted during account selection or verification; skipped after explicit continuation."})
                state["next_index"] = uncertain["index"] + 1
                state["in_flight"] = None
                state["halted"] = None
                _write(token, state)

        accounts = state["accounts"]
        results = state["results"]
        index = state["next_index"]
        processed = 0
        blocker: dict[str, Any] | None = None
        session_blocker: dict[str, Any] | None = None
        if retry_target is not None:
            remaining = chunk_budget_seconds - (time.monotonic() - call_started)
            if remaining < timeout:
                return _blocked_response(state, token)
            state["retry_in_flight"] = {**retry_target, "started": time.time()}
            state["halted"] = "current_account_recheck"
            _write(token, state)
            retry_started = time.monotonic()
            try:
                outcome = facebook_run(serial, action="current_location",
                                       account=retry_target["account"], timeout=float(timeout))
            except Exception as exc:
                outcome = {"state": "workflow_error", "detail": str(exc)}
            record = _make_record(retry_target["account"], outcome,
                                  time.monotonic() - retry_started)
            previous = (None if retry_target["interrupted"] else
                        state["results"][retry_target["index"]])
            previous_elapsed = (previous.get("elapsed_seconds") if previous else None)
            prior_attempt = ({key: previous.get(key) for key in
                              ("state", "elapsed_seconds", "location_retries", "timings",
                               "location_timing", "detail") if key in previous}
                             if previous else {"state": "interrupted_uncertain", "elapsed_seconds": None})
            record["prior_attempt"] = prior_attempt
            record["attempt_count"] = (previous.get("attempt_count", 1) + 1
                                       if previous else 2)
            record["retry_elapsed_seconds"] = record["elapsed_seconds"]
            if isinstance(previous_elapsed, (int, float)) and record["elapsed_seconds"] is not None:
                record["elapsed_seconds"] = round(previous_elapsed + record["elapsed_seconds"], 3)
            previous_retries = previous.get("location_retries", 0) if previous else 0
            current_retries = record.get("location_retries", 0)
            if isinstance(previous_retries, int) and isinstance(current_retries, int):
                record["location_retries"] = previous_retries + current_retries
            processed += 1
            if _verified(record):
                if retry_target["interrupted"]:
                    state["results"].append(record)
                    state["next_index"] = retry_target["index"] + 1
                else:
                    state["results"][retry_target["index"]] = record
                state["in_flight"] = None
                state["retry_in_flight"] = None
                state["halted"] = None
                state["last_blocker"] = None
                state["last_session_blocker"] = None
                state["updated"] = time.time()
                _write(token, state)
                accounts, results, index = state["accounts"], state["results"], state["next_index"]
            else:
                if not retry_target["interrupted"]:
                    state["results"][retry_target["index"]] = record
                state["last_blocker"] = record
                state["updated"] = time.time()
                _write(token, state)
                blocker = record

        while blocker is None and session_blocker is None and index < len(accounts) and processed < chunk_size:
            remaining = chunk_budget_seconds - (time.monotonic() - call_started)
            if remaining < 1:
                break
            if remaining < timeout:
                break
            name = accounts[index]
            account_started = time.monotonic()
            state["in_flight"] = {"account": name, "index": index, "started": time.time()}
            _write(token, state)
            try:
                outcome = facebook_run(serial, action="location", account=name,
                                       timeout=min(float(timeout), remaining))
            except Exception as exc:
                outcome = {"state": "workflow_error", "detail": str(exc),
                           "account_attempted": None, "action_uncertain": True}
            elapsed = round(time.monotonic() - account_started, 3)
            if outcome.get("action_uncertain") is True:
                state["halted"] = "workflow_error_requires_inspection"
                state["last_session_blocker"] = {
                    "state": outcome.get("state", "workflow_error"),
                    "detail": outcome.get("detail", "The workflow ended before its action stage was confirmed.")}
                state["updated"] = time.time()
                _write(token, state)
                session_blocker = state["last_session_blocker"]
                break
            if outcome.get("account_attempted", True) is False:
                state["in_flight"] = None
                state["halted"] = "device_or_session_blocker"
                state["last_session_blocker"] = {
                    key: outcome[key] for key in ("state", "detail", "pending_account") if key in outcome
                }
                state["updated"] = time.time()
                _write(token, state)
                session_blocker = state["last_session_blocker"]
                break
            record = _make_record(name, outcome, elapsed)
            # Persist every finished account outcome, but only a positively
            # identity-verified location is considered complete on resume.
            results.append(record)
            index += 1
            processed += 1
            state.update(results=results, next_index=index,
                         updated=time.time(), in_flight=None)
            _write(token, state)
            if _verified(record):
                continue
            blocker = record
            state["halted"] = "account_outcome_requires_inspection"
            state["last_blocker"] = record
            _write(token, state)
            break

        complete = (index >= len(accounts) and blocker is None and session_blocker is None
                    and len(results) == len(accounts)
                    and all(_verified(r)
                            for r in results))
        state_name = "complete" if complete else ("blocked" if blocker or session_blocker else ("partial" if index >= len(accounts) else "in_progress"))
        return {
            "device": serial, "state": state_name,
            "accounts": accounts, "results": results,
            **_remaining_fields(state),
            "complete": complete,
            "resume_token": None if complete or index >= len(accounts) and blocker is None and session_blocker is None else token,
            "blocker": blocker,
            "session_blocker": session_blocker,
            "timings": {
                "enumeration_seconds": round(float(state.get("enumeration_seconds", 0)), 3),
                "chunk_seconds": round(time.monotonic() - call_started, 3),
                "call_seconds": round(time.monotonic() - call_started, 3),
                "audit_wall_seconds": round(time.time() - float(state.get("created", time.time())), 3),
                "processed_this_call": processed,
                "completed_verified": sum(_verified(r) for r in results),
                "total_accounts": len(accounts),
            },
    }


def _make_record(name: str, outcome: dict[str, Any], elapsed: float) -> dict[str, Any]:
    actual_account = outcome.get("account")
    same_account = (isinstance(actual_account, str)
                    and " ".join(actual_account.split()).casefold()
                    == " ".join(name.split()).casefold())
    verified = (outcome.get("state") == "location"
                and outcome.get("identity_verified") is True
                and isinstance(outcome.get("location"), str)
                and bool(outcome["location"].strip()) and same_account)
    outcome_state = outcome.get("state", "unknown")
    if outcome_state == "location" and not same_account:
        outcome_state = "identity_mismatch"
    elif outcome_state == "location" and not verified:
        outcome_state = "invalid_result"
    record = {"account": name, "location": outcome.get("location") if verified else None,
              "identity_verified": verified, "state": outcome_state,
              "elapsed_seconds": round(elapsed, 3), "detail": outcome.get("detail")}
    for metric in ("location_retries", "timings", "location_timing"):
        if metric in outcome:
            record[metric] = outcome[metric]
    return record


def _blocked_response(state: dict[str, Any], token: str) -> dict[str, Any]:
    accounts = state.get("accounts", [])
    results = state.get("results", [])
    return {"device": state.get("device"), "state": "blocked",
            "accounts": accounts, "results": results,
            **_remaining_fields(state), "complete": False,
            "resume_token": token,
            "blocker": (None if state.get("halted") in ("device_or_session_blocker", "workflow_error_requires_inspection")
                        else state.get("last_blocker") or {
                        "state": state.get("halted", "interrupted_account_requires_inspection"),
                        "account": (state.get("in_flight") or {}).get("account"),
                        "detail": "Audit stopped at an uncertain account. Inspect the phone, then resume with retry_current=true or continue_after_blocker=true."}),
            "session_blocker": state.get("last_session_blocker"),
            "timings": {"enumeration_seconds": round(float(state.get("enumeration_seconds", 0)), 3),
                        "chunk_seconds": 0.0, "call_seconds": 0.0, "processed_this_call": 0,
                        "audit_wall_seconds": round(time.time() - float(state.get("created", time.time())), 3),
                        "completed_verified": sum(_verified(r) for r in results),
                        "total_accounts": len(accounts)}}


def _session_blocked_response(state: dict[str, Any], token: str,
                              observed: dict[str, Any], elapsed: float) -> dict[str, Any]:
    """Return a read-only readiness failure without changing checkpoint state."""
    blocker = {key: observed[key] for key in ("state", "detail", "pending_account") if key in observed}
    return {"device": state.get("device"), "state": "blocked",
            "accounts": state.get("accounts", []), "results": state.get("results", []),
            **_remaining_fields(state), "complete": False,
            "resume_token": token, "blocker": None, "session_blocker": blocker,
            "timings": {"enumeration_seconds": round(float(state.get("enumeration_seconds", 0)), 3),
                        "chunk_seconds": round(elapsed, 3), "call_seconds": round(elapsed, 3), "processed_this_call": 0,
                        "audit_wall_seconds": round(time.time() - float(state.get("created", time.time())), 3),
                        "completed_verified": sum(_verified(r) for r in state.get("results", [])),
                        "total_accounts": len(state.get("accounts", []))}}


def _remaining_fields(state: dict[str, Any]) -> dict[str, Any]:
    accounts = state.get("accounts", [])
    index = state.get("next_index", 0)
    uncertain = None
    marker = state.get("in_flight")
    if marker:
        uncertain = marker.get("account")
        if marker.get("index") == index:
            index += 1
    retry_marker = state.get("retry_in_flight")
    if retry_marker:
        uncertain = retry_marker.get("account")
    return {"next_index": state.get("next_index", 0),
            "unattempted_accounts": accounts[index:], "uncertain_account": uncertain}
