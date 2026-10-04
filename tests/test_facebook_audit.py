"""Resume and persistence tests for the chunked Facebook audit."""
import json

import pytest

from jev_use import facebook_audit, mcp_server


def outcome(serial, *, action, account=None, timeout=30):
    if action == "ready":
        return {"state": "ready", "screen": "feed"}
    if action == "accounts":
        return {"state": "accounts", "accounts": ["Alpha", "Beta", "Gamma"], "seconds": 1.25}
    return {"state": "location", "account": account, "location": "Lahore",
            "identity_verified": True, "account_attempted": True, "seconds": 2.0}


def test_audit_persists_verified_results_and_resumes_from_next_account(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    calls = []
    def fake(serial, **kwargs):
        calls.append((serial, kwargs["action"], kwargs.get("account")))
        return outcome(serial, **kwargs)

    first = facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=None,
                               facebook_run=fake)
    assert first["state"] == "in_progress"
    assert first["results"][0]["account"] == "Alpha"
    assert first["results"][0]["identity_verified"] is True
    assert first["resume_token"]
    stored = json.loads(facebook_audit._path(first["resume_token"]).read_text())
    assert stored["results"] == first["results"]

    second = facebook_audit.run("S", timeout=30, chunk_size=2,
                                resume_token=first["resume_token"], facebook_run=fake)
    assert second["state"] == "complete"
    assert second["complete"] is True
    assert [item["account"] for item in second["results"]] == ["Alpha", "Beta", "Gamma"]
    assert second["resume_token"] is None
    assert [call[2] for call in calls if call[1] == "location"] == ["Alpha", "Beta", "Gamma"]


@pytest.mark.parametrize("bad", ["../oops", "", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_invalid_or_unknown_resume_token_does_not_touch_phone(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    with pytest.raises(ValueError):
        facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=bad,
                           facebook_run=lambda *a, **kw: pytest.fail("must not run"))


def test_resume_token_is_bound_to_device(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    first = facebook_audit.run("S1", timeout=30, chunk_size=1, resume_token=None,
                               facebook_run=outcome)
    with pytest.raises(ValueError, match="different device"):
        facebook_audit.run("S2", timeout=30, chunk_size=1,
                           resume_token=first["resume_token"], facebook_run=outcome)


def test_audit_stops_and_records_failed_identity_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    def fake(serial, *, action, account=None, timeout=30):
        if action == "ready":
            return {"state": "ready", "screen": "feed"}
        return {"state": "accounts", "accounts": ["Alpha", "Beta"]} if action == "accounts" else {
            "state": "identity_mismatch", "detail": "Unexpected active account"}
    result = facebook_audit.run("S", timeout=30, chunk_size=2, resume_token=None,
                                facebook_run=fake)
    assert result["state"] == "blocked"
    assert result["blocker"]["state"] == "identity_mismatch"
    assert [record["account"] for record in result["results"]] == ["Alpha"]
    resumed = facebook_audit.run("S", timeout=30, chunk_size=2,
                                 resume_token=result["resume_token"], facebook_run=fake)
    assert resumed["state"] == "blocked"
    assert len(resumed["results"]) == 1
    continued = facebook_audit.run("S", timeout=30, chunk_size=2,
                                   resume_token=result["resume_token"], facebook_run=outcome,
                                   continue_after_blocker=True)
    assert continued["complete"] is False  # the failed Alpha outcome remains in the record
    assert [record["account"] for record in continued["results"]] == ["Alpha", "Beta"]


def test_retry_current_verifies_existing_account_without_reselection(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    calls = []
    def fake(serial, *, action, account=None, timeout=30):
        calls.append((action, account))
        if action == "ready":
            return {"state": "ready", "screen": "feed"}
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha", "Beta"]}
        if action == "location" and account == "Alpha":
            return {"state": "timeout", "account": account, "detail": "login still loading", "seconds": 46}
        return {"state": "location", "account": account, "location": "Lahore",
                "identity_verified": True, "seconds": 12, "location_retries": 1}

    first = facebook_audit.run("S", timeout=50, chunk_size=1, resume_token=None,
                               facebook_run=fake, chunk_budget_seconds=52)
    assert first["state"] == "blocked"
    assert first["results"][0]["state"] == "timeout"
    resumed = facebook_audit.run("S", timeout=50, chunk_size=2,
                                 resume_token=first["resume_token"], facebook_run=fake,
                                 chunk_budget_seconds=52, retry_current=True)
    assert calls == [("accounts", None), ("location", "Alpha"),
                     ("current_location", "Alpha"), ("location", "Beta")]
    assert resumed["complete"] is True
    assert [row["account"] for row in resumed["results"]] == ["Alpha", "Beta"]
    assert resumed["results"][0]["state"] == "location"
    assert resumed["results"][0]["attempt_count"] == 2
    assert resumed["results"][0]["prior_attempt"]["state"] == "timeout"
    assert resumed["results"][0]["location_retries"] == 1


def test_retry_current_identity_mismatch_stays_blocked_and_never_selects(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    def fake(serial, *, action, account=None, timeout=30):
        if action == "ready":
            return {"state": "ready", "screen": "menu"}
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha"]}
        if action == "location":
            return {"state": "timeout", "account": account}
        return {"state": "identity_mismatch", "account": "Other", "detail": "active identity differs"}
    first = facebook_audit.run("S", timeout=50, chunk_size=1, resume_token=None,
                               facebook_run=fake, chunk_budget_seconds=52)
    second = facebook_audit.run("S", timeout=50, chunk_size=1,
                                resume_token=first["resume_token"], facebook_run=fake,
                                chunk_budget_seconds=52, retry_current=True)
    assert second["state"] == "blocked"
    assert second["results"][0]["state"] == "identity_mismatch"
    assert second["results"][0]["location"] is None


def test_retry_current_recovers_interrupted_selection_without_reselecting(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    token = "123e4567-e89b-42d3-a456-426614174000"
    facebook_audit._write(token, {"token": token, "device": "S", "accounts": ["Alpha", "Beta"],
                                  "next_index": 0, "results": [], "in_flight": {
                                      "account": "Alpha", "index": 0, "started": 0},
                                  "enumeration_seconds": 1, "created": 1})
    calls = []
    def fake(serial, *, action, account=None, timeout=30):
        calls.append((action, account))
        if action == "current_location":
            return {"state": "location", "account": account, "location": "Lahore",
                    "identity_verified": True}
        return {"state": "location", "account": account, "location": "Manila",
                "identity_verified": True}
    result = facebook_audit.run("S", timeout=50, chunk_size=2, resume_token=token,
                                facebook_run=fake, chunk_budget_seconds=52,
                                retry_current=True)
    assert [action for action, _ in calls] == ["current_location", "location"]
    assert calls[0][1] == "Alpha" and calls[1][1] == "Beta"
    assert result["complete"] is True
    assert result["results"][0]["prior_attempt"]["state"] == "interrupted_uncertain"


def test_final_failed_account_is_never_reported_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    def fake(serial, *, action, account=None, timeout=30):
        return {"state": "accounts", "accounts": ["Only"]} if action == "accounts" else {
            "state": "timeout", "detail": "page did not load"}
    result = facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=None,
                                facebook_run=fake)
    assert result["complete"] is False
    assert result["state"] == "blocked"
    assert result["resume_token"]
    assert result["results"][0]["location"] is None


@pytest.mark.parametrize("options", [{"chunk_size": 0}, {"chunk_size": 6},
                                      {"timeout": 60, "chunk_budget_seconds": 55},
                                      {"retry_current": True},
                                      {"retry_current": True, "continue_after_blocker": True}])
def test_incompatible_audit_bounds_fail_before_device_discovery(monkeypatch, options):
    monkeypatch.setattr(mcp_server, "android_device", lambda args: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        mcp_server.tool_android_facebook({"action": "audit", **options})


def test_location_claim_with_wrong_identity_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    def fake(serial, *, action, account=None, timeout=30):
        if action == "ready":
            return {"state": "ready", "screen": "feed"}
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha"]}
        return {"state": "location", "account": "Beta", "location": "Lahore",
                "identity_verified": True}
    result = facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=None,
                                facebook_run=fake)
    assert result["results"][0]["state"] == "identity_mismatch"
    assert result["results"][0]["identity_verified"] is False
    assert result["results"][0]["location"] is None
    assert result["complete"] is False


def test_interrupted_selection_is_not_replayed_and_requires_explicit_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    token = "123e4567-e89b-42d3-a456-426614174000"
    facebook_audit._write(token, {"token": token, "device": "S", "accounts": ["Alpha", "Beta"],
                                  "next_index": 0, "results": [], "in_flight": {
                                      "account": "Alpha", "index": 0, "started": 0},
                                  "enumeration_seconds": 1, "created": 1})
    calls = []
    def fake(serial, *, action, account=None, timeout=30):
        calls.append((action, account))
        if action == "ready":
            return {"state": "ready", "screen": "feed"}
        return {"state": "location", "account": account, "location": "Lahore", "identity_verified": True}
    blocked = facebook_audit.run("S", timeout=30, chunk_size=2, resume_token=token,
                                 facebook_run=fake)
    assert blocked["state"] == "blocked"
    assert calls == []
    continued = facebook_audit.run("S", timeout=30, chunk_size=2, resume_token=token,
                                   facebook_run=fake, continue_after_blocker=True)
    assert calls == [("ready", None), ("location", "Beta")]
    assert [record["state"] for record in continued["results"]] == ["interrupted_uncertain", "location"]
    assert continued["complete"] is False


def test_chunk_budget_does_not_start_an_account_without_its_full_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    elapsed = [0.0]
    monkeypatch.setattr(facebook_audit.time, "monotonic", lambda: elapsed[0])
    location_calls = []
    def fake(serial, *, action, account=None, timeout=30):
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha", "Beta"], "seconds": 0}
        location_calls.append(account)
        elapsed[0] += 45
        return {"state": "location", "account": account, "location": "Lahore", "identity_verified": True}
    result = facebook_audit.run("S", timeout=30, chunk_size=2, chunk_budget_seconds=55,
                                resume_token=None, facebook_run=fake)
    assert location_calls == ["Alpha"]
    assert result["state"] == "in_progress"
    assert result["timings"]["processed_this_call"] == 1


def test_preselection_session_blocker_does_not_consume_account_and_preflight_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    ready_states = [{"state": "pending_login", "pending_account": "Alpha",
                     "detail": "Still logging in as Alpha"},
                    {"state": "ready", "screen": "feed"}]
    calls = []
    def fake(serial, *, action, account=None, timeout=30):
        calls.append((action, account))
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha", "Beta"]}
        if action == "location":
            return {"state": "pending_login", "detail": "Still logging in as Alpha",
                    "account_attempted": False, "pending_account": "Alpha"}
        if action == "ready":
            return ready_states.pop(0)
        pytest.fail(f"unexpected action: {action}")

    first = facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=None,
                               facebook_run=fake)
    assert first["state"] == "blocked"
    assert first["results"] == []
    assert first["next_index"] == 0
    assert first["unattempted_accounts"] == ["Alpha", "Beta"]
    assert first["session_blocker"]["pending_account"] == "Alpha"
    path = facebook_audit._path(first["resume_token"])
    before = path.read_text()

    still_blocked = facebook_audit.run("S", timeout=30, chunk_size=1,
                                       resume_token=first["resume_token"],
                                       facebook_run=fake, continue_after_blocker=True)
    assert still_blocked["session_blocker"]["pending_account"] == "Alpha"
    assert path.read_text() == before
    assert still_blocked["results"] == [] and still_blocked["next_index"] == 0

    # A read-only readiness check now passes; Alpha is still the next account.
    original = fake
    def ready_then_succeed(serial, *, action, account=None, timeout=30):
        if action == "ready":
            return {"state": "ready", "screen": "feed"}
        if action == "location":
            return {"state": "location", "account": account, "location": "Lahore",
                    "identity_verified": True, "account_attempted": True}
        return original(serial, action=action, account=account, timeout=timeout)
    resumed = facebook_audit.run("S", timeout=30, chunk_size=1,
                                 resume_token=first["resume_token"],
                                 facebook_run=ready_then_succeed,
                                 continue_after_blocker=True)
    assert resumed["results"][0]["account"] == "Alpha"
    assert resumed["complete"] is False
    assert resumed["unattempted_accounts"] == ["Beta"]


def test_unexpected_workflow_exception_preserves_inflight_as_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    def fail(serial, *, action, account=None, timeout=30):
        if action == "accounts":
            return {"state": "accounts", "accounts": ["Alpha"]}
        raise RuntimeError("transport died during selection")
    first = facebook_audit.run("S", timeout=30, chunk_size=1, resume_token=None,
                               facebook_run=fail)
    saved = json.loads(facebook_audit._path(first["resume_token"]).read_text())
    assert first["results"] == []
    assert first["next_index"] == 0
    assert saved["in_flight"]["account"] == "Alpha"
    assert first["session_blocker"]["state"] == "workflow_error"


def test_audit_tool_routes_to_module_and_validates_chunk_before_device(monkeypatch):
    monkeypatch.setattr(mcp_server, "android_device", lambda args: pytest.fail("must validate"))
    with pytest.raises(ValueError):
        mcp_server.tool_android_facebook({"action": "audit", "chunk_size": True})

    monkeypatch.setattr(mcp_server, "android_device", lambda args: type("D", (), {"serial": "S"})())
    monkeypatch.setattr(mcp_server.facebook_audit, "validate_resume_token", lambda token: None)
    seen = {}
    def audit(serial, **kwargs):
        seen.update(serial=serial, **kwargs)
        return {"state": "in_progress", "complete": False}
    monkeypatch.setattr(mcp_server.facebook_audit, "run", audit)
    result = json.loads(mcp_server.tool_android_facebook({"action": "audit", "chunk_size": 2,
                                                          "resume_token": "123e4567-e89b-42d3-a456-426614174000", "timeout": 30}))
    assert result["state"] == "in_progress"
    assert seen["serial"] == "S"
    assert seen["chunk_size"] == 2
    assert seen["resume_token"] == "123e4567-e89b-42d3-a456-426614174000"


def test_facebook_audit_schema_and_mobile_prompt_expose_resume_flow():
    tool = next(item for item in mcp_server.TOOLS if item["name"] == "android_facebook")
    props = tool["inputSchema"]["properties"]
    assert "audit" in props["action"]["enum"]
    assert props["chunk_size"]["default"] == 2
    assert props["timeout"]["default"] == facebook_audit.DEFAULT_ACCOUNT_TIMEOUT == 50
    assert "resume_token" in props
    assert props["chunk_budget_seconds"]["default"] == 52
    assert "continue_after_blocker" in props
    assert "retry_current" in props
    assert "resume_token" in mcp_server.MOBILE_PROMPT_TEMPLATE
    assert "unattempted_accounts" in mcp_server.MOBILE_PROMPT_TEMPLATE


def test_mcp_audit_normalizes_canonical_string_arguments_before_device(monkeypatch):
    monkeypatch.setattr(mcp_server, "android_device", lambda args: type("D", (), {"serial": "S"})())
    token = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setattr(mcp_server.facebook_audit, "validate_resume_token", lambda value: None)
    seen = {}
    def run(serial, **kwargs):
        seen.update(serial=serial, **kwargs)
        return {"state": "in_progress"}
    monkeypatch.setattr(mcp_server.facebook_audit, "run", run)
    mcp_server.tool_android_facebook({"action": "audit", "chunk_size": " 2 ",
                                      "timeout": "50", "chunk_budget_seconds": "52",
                                      "resume_token": token,
                                      "continue_after_blocker": "True", "retry_current": "false"})
    assert seen["chunk_size"] == 2 and type(seen["chunk_size"]) is int
    assert seen["timeout"] == 50.0 and seen["chunk_budget_seconds"] == 52.0
    assert seen["continue_after_blocker"] is True and seen["retry_current"] is False


@pytest.mark.parametrize("args", [
    {"chunk_size": "2.0"}, {"chunk_size": "True"}, {"chunk_size": True},
    {"continue_after_blocker": "yes"}, {"retry_current": "1"},
    {"timeout": "NaN"}, {"chunk_budget_seconds": "Infinity"},
])
def test_invalid_canonical_audit_strings_fail_before_device(monkeypatch, args):
    monkeypatch.setattr(mcp_server, "android_device", lambda _: pytest.fail("must reject before adb"))
    with pytest.raises(ValueError):
        mcp_server.tool_android_facebook({"action": "audit", **args})
