import os

import pytest

from jev_use import facebook_flow_state as flow


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(flow.host, "work_root", lambda: tmp_path)
    return tmp_path


def make_flow():
    return flow.create(serial="device-1", account="Shyam Desai",
                       place="Manila, Philippines", audience="Public")


def test_create_load_and_advance_checkpoint_stages(state_root):
    created = make_flow()
    assert created["stage"] == "created"
    assert flow.load(created["token"]) == created
    assert flow._path(created["token"]).parent == state_root / "facebook-flows"

    logging_in = flow.save(created["token"], "logging_in", expected_stage="created")
    # A restart at this checkpoint waits for the requested account's existing
    # login transition; it does not carry any request to select the account again.
    assert flow.load(created["token"])["stage"] == "logging_in"
    assert logging_in["account"] == created["account"]
    identity = flow.save(created["token"], "identity_verified",
                         expected_stage="logging_in")
    location = flow.save(created["token"], "location_checked",
                         expected_stage="identity_verified",
                         evidence={"primary_location": "Muridke, Punjab 39",
                                   "primary_location_state": "location",
                                   "location_seconds": 6.1})
    composing = flow.save(created["token"], "composing",
                          expected_stage="location_checked")
    pre_submit = flow.save(created["token"], "pre-submit",
                           expected_stage="composing")

    # The caller persists this before tapping Publish; a resume sees that the
    # irreversible action may already have happened and cannot submit again.
    submitting = flow.save(created["token"], "submitting",
                           expected_stage="pre-submit",
                           evidence={"post_signature": "check-in:Manila"})
    assert flow.load(created["token"])["stage"] == "submitting"
    assert submitting["account"] == "Shyam Desai"
    posted = flow.save(created["token"], "posted", expected_stage="submitting")
    logging_out = flow.save(created["token"], "logging_out", expected_stage="posted")
    logged_out = flow.save(created["token"], "logged_out", expected_stage="logging_out")
    assert logged_out["stage"] == "logged_out"
    assert logged_out["place"] == "Manila, Philippines"
    assert logging_out["audience"] == "Public"
    assert posted["serial"] == "device-1"
    assert identity["stage"] == "identity_verified"
    assert location["evidence"]["primary_location_state"] == "location"
    assert location["evidence"]["location_seconds"] == 6.1
    assert composing["stage"] == "composing"
    assert pre_submit["evidence"] == location["evidence"]
    assert logged_out["evidence"]["post_signature"] == "check-in:Manila"


@pytest.mark.parametrize("value", ["../escape", "", "not-a-uuid", "0" * 36])
def test_invalid_token_rejected_before_path_access(state_root, value):
    with pytest.raises(ValueError, match="token is invalid"):
        flow.load(value)
    assert not list(state_root.rglob("*.json"))


def test_unknown_well_formed_token_is_rejected(state_root):
    token = "123e4567-e89b-42d3-a456-426614174000"
    with pytest.raises(ValueError, match="not found"):
        flow.load(token)


@pytest.mark.parametrize("field,value", [
    ("serial", " "), ("account", ""), ("place", None), ("audience", "\t"),
])
def test_create_requires_nonempty_binding_fields(state_root, field, value):
    args = {"serial": "S", "account": "Shyam Desai",
            "place": "Manila, Philippines", "audience": "Public"}
    args[field] = value
    with pytest.raises(ValueError, match=f"{field} must be a nonempty string"):
        flow.create(**args)


@pytest.mark.parametrize("stage", ["created", "identity_verified", "location_checked",
                                    "pre-submit", "posted", "logging_out", "logged_out", "unknown"])
def test_save_rejects_skipped_repeated_or_invalid_stage(state_root, stage):
    checkpoint = make_flow()
    with pytest.raises(ValueError):
        flow.save(checkpoint["token"], stage)
    assert flow.load(checkpoint["token"])["stage"] == "created"


def test_save_rejects_repeating_current_stage(state_root):
    checkpoint = make_flow()
    flow.save(checkpoint["token"], "logging_in")
    with pytest.raises(ValueError, match="exactly one stage"):
        flow.save(checkpoint["token"], "logging_in")
    assert flow.load(checkpoint["token"])["stage"] == "logging_in"


def test_save_rejects_stale_expected_stage(state_root):
    checkpoint = make_flow()
    flow.save(checkpoint["token"], "logging_in")
    with pytest.raises(ValueError, match="stage changed"):
        flow.save(checkpoint["token"], "identity_verified", expected_stage="created")
    assert flow.load(checkpoint["token"])["stage"] == "logging_in"


def test_bindings_stay_fixed_and_secret_fields_cannot_be_saved(state_root):
    checkpoint = make_flow()
    state = flow.save(checkpoint["token"], "logging_in")
    assert {key: state[key] for key in ("serial", "account", "place", "audience")} == {
        key: checkpoint[key] for key in ("serial", "account", "place", "audience")
    }
    stored = flow.load(checkpoint["token"])
    stored["password"] = "should never be journaled"
    with pytest.raises(ValueError, match="failed validation"):
        flow._write(checkpoint["token"], stored)


@pytest.mark.parametrize("evidence", [
    {"password": "do-not-store"}, {"access_key": "do-not-store"},
    {"credentials": "do-not-store"}, {"nested": {"value": "not scalar"}},
    {"oversized": "x" * 2049},
])
def test_evidence_rejects_credentials_and_non_scalar_or_oversized_values(state_root, evidence):
    checkpoint = make_flow()
    with pytest.raises(ValueError, match="flow evidence"):
        flow.save(checkpoint["token"], "logging_in", evidence=evidence)
    assert flow.load(checkpoint["token"])["stage"] == "created"


def test_evidence_merges_across_stages_and_stays_small(state_root):
    checkpoint = make_flow()
    flow.save(checkpoint["token"], "logging_in",
              evidence={"login_state": "pending", "attempt": 1})
    next_state = flow.save(checkpoint["token"], "identity_verified",
                            evidence={"login_state": "complete", "identity_verified": True})
    assert next_state["evidence"] == {"login_state": "complete", "attempt": 1,
                                      "identity_verified": True}


def test_write_uses_private_directory_and_atomic_final_file(state_root):
    checkpoint = make_flow()
    directory = state_root / "facebook-flows"
    path = flow._path(checkpoint["token"])
    assert path.is_file()
    assert not list(directory.glob(".flow-*"))
    if os.name == "posix":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600


def test_same_job_recovers_existing_checkpoint_after_lost_response(state_root):
    params = dict(serial="S", account="Shyam Desai", place="Manila, Philippines",
                  audience="Public", run_id="manila-test-1")
    first = flow.create(**params)
    advanced = flow.save(first["token"], "logging_in")
    assert flow.create(**params) == advanced
    with pytest.raises(ValueError, match="different flow parameters"):
        flow.create(**{**params, "place": "Another City"})
