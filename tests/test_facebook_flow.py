import pytest

from jev_use import android, facebook_flow, facebook_flow_state

@pytest.fixture
def flow_root(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_flow_state.host, "work_root", lambda: tmp_path)
    return tmp_path


def checkpoint(stage="created"):
    state = facebook_flow_state.create(
        serial="device-1", account="Shyam Desai",
        place="Manila, Philippines", audience="Public")
    while state["stage"] != stage:
        next_stage = facebook_flow_state.STAGES[
            facebook_flow_state.STAGES.index(state["stage"]) + 1]
        state = facebook_flow_state.save(state["token"], next_stage)
    return state


class FakeWorkflow:
    instances = []
    observed = None

    def __init__(self, serial, timeout):
        self.serial = serial
        self.timeout = timeout
        self.deadline = None
        self.timings = {}
        self.calls = []
        self.instances.append(self)

    def observe(self, **kwargs):
        self.calls.append(("observe", kwargs))
        return self.observed

    def select(self, account, observation=None):
        self.calls.append(("select", account))
        return account

    def bootstrap_saved_card(self, account, observation=None):
        self.calls.append(("bootstrap", account))
        return account, self.observed

    def wait_pending_login(self, account, observation):
        self.calls.append(("wait_pending_login", account))
        return observation

    def menu(self, observation=None):
        self.calls.append(("menu",))
        return observation or self.observed


class Screen:
    def __init__(self, text="", targets=None, fields=None, nodes=None):
        self.text = text
        self.targets = targets or []
        self.fields = fields or []
        self.nodes = nodes or []
        self.screen = (1080, 2160)
        self.foreground = "com.facebook.katana/.Main"

    def read(self):
        return self.text

    def by_id(self, node_id):
        return next((target for target in self.targets
                     if str(target.get("id")) == str(node_id)), None)


class SimpleNode:
    def __init__(self, ref, checked=False):
        self.ref = ref
        self.checked = checked
        self.text = ""


class DraftWorkflow:
    serial = "device-1"

    def ensure_time(self):
        pass

    def __init__(self, observations=()):
        self.observations = list(observations)
        self.taps = []

    def wait(self, predicate, detail):
        while self.observations:
            observation = self.observations.pop(0)
            if predicate(observation):
                return observation
        raise AssertionError(detail)


class PostingWorkflow(DraftWorkflow):
    def __init__(self, observations):
        super().__init__(observations)
        self.timings = {}

    def observe(self, **kwargs):
        return self.observations.pop(0)

    def menu(self, observation=None):
        return observation


def _published(audience="Public", account="Shyam Desai"):
    label = f"{account} is in Manila, Philippines., Just now•Shared with: {audience}"
    return Screen(text=label, targets=[{"role": "viewgroup", "label": label}])


def test_submit_journals_before_click_and_verifies_same_public_feed_card(flow_root, monkeypatch):
    state = checkpoint("pre-submit")
    workflow = PostingWorkflow([_preview(), _published()])
    stages = []
    monkeypatch.setattr(android, "tap", lambda *args: stages.append(
        facebook_flow_state.load(state["token"])["stage"]))
    posted = facebook_flow.confirm_post(workflow, token=state["token"])
    assert stages == ["submitting"]
    assert posted["stage"] == "posted"
    assert posted["evidence"]["post_verified"] is True
    with pytest.raises(facebook_flow.Blocked, match="not submitted again"):
        facebook_flow.confirm_post(workflow, token=state["token"])


def test_wrong_preview_audience_never_consumes_submission_or_clicks(flow_root, monkeypatch):
    state = checkpoint("pre-submit")
    workflow = PostingWorkflow([_preview(audience="Friends")])
    monkeypatch.setattr(android, "tap", lambda *args: pytest.fail("Do not publish to wrong audience"))
    with pytest.raises(facebook_flow.Blocked):
        facebook_flow.confirm_post(workflow, token=state["token"])
    assert facebook_flow_state.load(state["token"])["stage"] == "pre-submit"


def test_publication_evidence_must_match_account_visibility_and_actual_feed():
    assert facebook_flow._published_card(_published(), account="Shyam Desai",
                                        place="Manila, Philippines", audience="Public")
    for screen in (_preview(), _published("Friends"), _published(account="Another Account")):
        assert not facebook_flow._published_card(screen, account="Shyam Desai",
                                                place="Manila, Philippines", audience="Public")


def test_logout_journals_before_first_click_and_verifies_landing(flow_root, monkeypatch):
    state = checkpoint("submitting")
    state = facebook_flow_state.save(state["token"], "posted", evidence={"post_verified": True})
    menu = Screen(text="Shyam Desai\nMenu", targets=[
        {"role": "button", "label": "Log out", "centred": (10, 10)}])
    dialog = Screen(text="Log out of your account?", targets=[
        {"role": "button", "label": "LOG OUT", "centred": (20, 20)}])
    landing = Screen(text="Facebook from Meta\nUse another profile\nCreate new account")
    monkeypatch.setattr(facebook_flow.facebook_android, "is_saved_login_landing",
                        lambda obs: "Facebook from Meta" in obs.read())
    stages = []
    monkeypatch.setattr(android, "tap", lambda *args: stages.append(
        facebook_flow_state.load(state["token"])["stage"]))
    result = facebook_flow.logout(PostingWorkflow([menu, dialog, landing]), token=state["token"])
    assert stages == ["logging_out", "logging_out"]
    assert result["stage"] == "logged_out"
    assert result["evidence"]["logout_verified"] is True


def test_logout_resume_observes_completed_logout_without_repeating_tap(flow_root, monkeypatch):
    state = checkpoint("submitting")
    state = facebook_flow_state.save(state["token"], "posted", evidence={"post_verified": True})
    state = facebook_flow_state.save(state["token"], "logging_out")
    landing = Screen(text="saved landing")
    monkeypatch.setattr(facebook_flow.facebook_android, "is_saved_login_landing", lambda obs: True)
    monkeypatch.setattr(android, "tap", lambda *args: pytest.fail("Logout was already completed"))
    result = facebook_flow.logout(PostingWorkflow([landing]), token=state["token"])
    assert result["stage"] == "logged_out"


def test_slow_login_preserves_unattempted_location_for_next_chunk(flow_root, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(facebook_flow.time, "monotonic", lambda: clock[0])

    def slow_select(self, account, observation=None):
        clock[0] = 45.0
        return account

    monkeypatch.setattr(FakeWorkflow, "select", slow_select)
    monkeypatch.setattr(facebook_flow.facebook_android, "run",
                        lambda *a, **k: pytest.fail("Do not attempt location without time to inspect it"))
    result = facebook_flow.run("device-1", "Shyam Desai", timeout=55)
    assert result["state"] == "timeout"
    assert result["stage"] == "identity_verified"
    assert result["primary_location_state"] is None


@pytest.fixture(autouse=True)
def fake_workflow(monkeypatch):
    FakeWorkflow.instances = []
    FakeWorkflow.observed = Screen(text="Facebook")
    monkeypatch.setattr(facebook_flow.facebook_android, "Workflow", FakeWorkflow)
    monkeypatch.setattr(facebook_flow.facebook_android, "is_saved_login_landing", lambda obs: False)
    monkeypatch.setattr(facebook_flow.facebook_android, "identity", lambda obs: getattr(obs, "account", "Shyam Desai"))


def test_created_flow_journals_login_before_selection_then_location(flow_root, monkeypatch):
    outcome = {"state": "location", "location": "Muridke, Punjab 39",
               "identity_verified": True, "seconds": 6.2, "timings": {"reads": 1.0}}
    monkeypatch.setattr(facebook_flow.facebook_android, "run", lambda *a, **k: outcome)

    result = facebook_flow.run("device-1", "Shyam Desai", publish=False)
    assert result["state"] == "location_checked"
    assert result["stage"] == "location_checked"
    assert result["evidence"]["primary_location"] == "Muridke, Punjab 39"
    assert result["evidence"]["identity_verified"] is True
    assert FakeWorkflow.instances[0].calls[0][0] == "observe"
    assert FakeWorkflow.instances[0].calls[1] == ("select", "Shyam Desai")
    stored = facebook_flow_state.load(result["resume_token"])
    assert stored["stage"] == "location_checked"


def test_resumed_login_waits_and_verifies_without_reselecting(flow_root, monkeypatch):
    state = checkpoint("logging_in")
    location_calls = []
    monkeypatch.setattr(facebook_flow.facebook_android, "run",
                        lambda *a, **k: location_calls.append((a, k)) or {
                            "state": "location", "location": "Manila, Metro Manila",
                            "identity_verified": True, "seconds": 3.0})

    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"])
    calls = FakeWorkflow.instances[0].calls
    assert result["stage"] == "location_checked"
    assert [call[0] for call in calls] == ["observe", "wait_pending_login", "menu"]
    assert not any(call[0] in ("select", "bootstrap") for call in calls)
    assert len(location_calls) == 1


def test_identity_mismatch_does_not_read_location_or_reselect(flow_root, monkeypatch):
    state = checkpoint("logging_in")
    monkeypatch.setattr(facebook_flow.facebook_android, "identity",
                        lambda obs: "Another Account")
    location_calls = []
    monkeypatch.setattr(facebook_flow.facebook_android, "run",
                        lambda *a, **k: location_calls.append((a, k)))

    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"])
    assert result["state"] == "identity_mismatch"
    assert result["stage"] == "logging_in"
    assert location_calls == []
    assert not any(call[0] in ("select", "bootstrap") for call in FakeWorkflow.instances[0].calls)


@pytest.mark.parametrize("stage", ["submitting"])
def test_uncertain_irreversible_stages_never_repeat_actions(flow_root, monkeypatch, stage):
    state = checkpoint(stage)
    monkeypatch.setattr(facebook_flow, "confirm_post",
                        lambda *a, **k: pytest.fail("Publish must never be replayed"))
    monkeypatch.setattr(facebook_flow, "logout",
                        lambda *a, **k: pytest.fail("logout must never be replayed"))

    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"],
                               publish=True)
    assert result["state"] == "requires_inspection"
    assert result["stage"] == stage
    assert FakeWorkflow.instances == []


def test_logged_out_checkpoint_returns_without_phone_actions(flow_root):
    state = checkpoint("logged_out")
    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"],
                               publish=True)
    assert result["state"] == "complete"
    assert result["stage"] == "logged_out"
    assert FakeWorkflow.instances == []


def test_resume_token_is_bound_to_all_flow_arguments(flow_root):
    state = checkpoint()
    with pytest.raises(ValueError, match="bound to a different"):
        facebook_flow.run("device-1", "Shyam Desai", place="Cebu, Philippines",
                          resume_token=state["token"])


def test_publish_request_stops_safely_until_composer_controls_are_grounded(flow_root):
    state = checkpoint("location_checked")
    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"],
                               publish=True)
    assert result["state"] == "unsupported_ui"
    assert result["stage"] == "composing"
    assert result["evidence"]["publish_enabled"] is True
    assert "Home tab" in result["detail"]


def test_resuming_composing_preserves_recorded_publish_authorization(flow_root):
    state = checkpoint("location_checked")
    state = facebook_flow_state.save(state["token"], "composing",
                                     evidence={"publish_enabled": True})
    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"],
                               publish=False)
    assert result["state"] == "unsupported_ui"
    assert result["stage"] == "composing"
    assert result["evidence"]["publish_enabled"] is True


def test_unavailable_location_is_journaled_then_authorized_post_may_continue(flow_root, monkeypatch):
    state = checkpoint("identity_verified")
    monkeypatch.setattr(facebook_flow.facebook_android, "run", lambda *a, **k: {
        "state": "unknown", "detail": "Page did not render.",
        "identity_verified": False, "seconds": 10,
    })
    result = facebook_flow.run("device-1", "Shyam Desai", resume_token=state["token"],
                               publish=True)
    assert result["state"] == "unsupported_ui"
    assert result["stage"] == "composing"
    assert result["evidence"]["primary_location_state"] == "unknown"
    assert "primary_location" not in result["evidence"]


def test_location_card_requires_exact_place_country_and_checkin_signature():
    valid = {"role": "viewgroup", "label":
             "Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, Philippines, 17"}
    prefix_impostor = {"role": "viewgroup", "label":
                       "Manila, Philippines, Manila, Philippines North, 17M check-ins, Manila, Philippines"}
    other_country = {"role": "viewgroup", "label":
                     "Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, USA"}
    more_actions = {"role": "viewgroup", "label":
                    "Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, Philippines, More actions"}
    obs = Screen(targets=[valid, prefix_impostor, other_country, more_actions])
    assert facebook_flow._matching_place_card(obs, "Manila, Philippines") == [valid]


def test_location_card_ambiguity_is_not_resolved_by_prefix_or_position():
    a = {"role": "viewgroup", "label":
         "Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, Philippines, 17"}
    b = {"role": "viewgroup", "label":
         "Manila, Philippines, Manila, Philippines, 18M check-ins, Manila, Philippines, 17"}
    assert len(facebook_flow._matching_place_card(Screen(targets=[a, b]),
                                                 "Manila, Philippines")) == 2


def test_home_tab_uses_exact_control_and_needs_complete_row_for_geometry():
    exact = {"label": "Home, tab 1 of 6", "bounds": (0, 100, 180, 160)}
    assert facebook_flow._home_tab(Screen(targets=[exact])) is exact

    partial = Screen(targets=[{"label": "Menu", "bounds": (900, 100, 1080, 160)}])
    assert facebook_flow._home_tab(partial) is None


def test_wrong_composer_account_fails_closed():
    obs = Screen(text="New post\nOther Account\nLocation. 3 of 4")
    with pytest.raises(facebook_flow.Blocked) as error:
        facebook_flow._require_composer_account(obs, "Shyam Desai")
    assert error.value.state == "identity_mismatch"


def _preview(account="Shyam Desai", place="Manila, Philippines", audience="Public",
             include_post=True):
    targets = [
        {"id": "preview", "role": "button", "label":
         f"{account} is at {place}., Just now, Map, {place}", "centred": (10, 10)},
        {"id": "audience", "role": "button", "label": f"Post audience, {audience}",
         "centred": (20, 20)},
    ]
    if include_post:
        targets.append({"id": "post", "role": "button", "label": "Post",
                        "centred": (30, 30)})
    return Screen(text=f"New post\n{account}\n{audience}", targets=targets)


def test_preview_must_verify_public_audience_and_enabled_post_before_pre_submit(flow_root, monkeypatch):
    state = checkpoint("composing")
    tapped = []
    monkeypatch.setattr(android, "tap", lambda *args: tapped.append(args))

    with pytest.raises(facebook_flow.Blocked) as error:
        facebook_flow._save_ready_draft(
            state["token"], DraftWorkflow(), _preview(audience="Friends"),
            account="Shyam Desai", place="Manila, Philippines", audience="Public")
    assert error.value.state == "audience_unverified"
    assert facebook_flow_state.load(state["token"])["stage"] == "composing"

    with pytest.raises(facebook_flow.Blocked) as error:
        facebook_flow._save_ready_draft(
            state["token"], DraftWorkflow(), _preview(include_post=False),
            account="Shyam Desai", place="Manila, Philippines", audience="Public")
    assert error.value.state == "unsupported_ui"
    assert facebook_flow_state.load(state["token"])["stage"] == "composing"

    ready = facebook_flow._save_ready_draft(
        state["token"], DraftWorkflow(), _preview(), account="Shyam Desai",
        place="Manila, Philippines", audience="Public")
    assert ready["stage"] == "pre-submit"
    assert ready["evidence"]["draft_ready"] is True
    assert ready["evidence"]["draft_audience"] == "Public"
    assert tapped == []  # Preparing a draft never presses Post.


def test_public_audience_is_selected_and_verified_without_setting_default(monkeypatch):
    radio = {"id": "1", "role": "radio",
             "label": "Public, Anyone on or off Facebook", "centred": (20, 20)}
    done = {"id": "2", "role": "button", "label": "Done", "centred": (30, 30)}
    selected_radio = SimpleNode(ref=1, checked=True)
    picker = Screen(text="Who can see your post?\nSet as default",
                    targets=[radio, done], nodes=[])
    selected = Screen(text="Who can see your post?\nSet as default",
                      targets=[radio, done], nodes=[selected_radio])
    preview = _preview(audience="Public")
    workflow = DraftWorkflow([picker, selected, preview])
    taps = []
    monkeypatch.setattr(android, "tap", lambda serial, x, y: taps.append((x, y)))
    friends_preview = _preview(audience="Friends")

    result = facebook_flow._require_public_audience(
        workflow, friends_preview, account="Shyam Desai", place="Manila, Philippines")
    assert result is preview
    assert taps == [(20, 20), (20, 20), (30, 30)]


def test_search_and_selected_public_read_from_real_observation_nodes():
    obs = android.Observation(
        serial="device-1",
        nodes=android.parse_nodes('''<hierarchy>
          <node class="android.widget.EditText" text="" content-desc="Search"
          package="com.facebook.katana" clickable="true" enabled="true"
          bounds="[10,100][900,180]" />
          <node class="android.widget.RadioButton" text="Public, Anyone on or off Facebook"
          package="com.facebook.katana" clickable="true" checkable="true" checked="true"
          enabled="true" bounds="[10,250][900,330]" />
        </hierarchy>'''),
        screen=(1080, 2160), foreground="com.facebook.katana/.Main")

    search = facebook_flow._search_field(obs)
    public = [target for target in obs.targets
              if target["label"] == "Public, Anyone on or off Facebook"]
    assert search is not None
    assert isinstance(obs.by_id(search["id"]), dict)  # by_id returns a candidate
    assert facebook_flow._node_for_target(obs, search).text == ""
    assert len(public) == 1
    assert facebook_flow._node_checked(obs, public[0]) is True
