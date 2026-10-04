"""Tests for the Android engine.

Everything here runs without a phone attached: the dump is parsed from a string,
the device table from a faked `adb` response. The point is that the parts which
are easy to get subtly wrong — coordinate parsing, candidate gating, shell
escaping, device selection — are pinned down on a machine with no device plugged
in, because that is the normal state of CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jev_use import android
from jev_use.choosers import Decision


# -- fixtures ---------------------------------------------------------------

DUMP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example.app" content-desc="" clickable="false" enabled="true"
        bounds="[0,0][1080,2160]">
    <node index="0" text="Sign in" resource-id="com.example.app:id/signin"
          class="android.widget.Button" package="com.example.app" content-desc=""
          clickable="true" enabled="true" bounds="[100,1000][500,1100]" />
    <node index="1" text="Email" resource-id="com.example.app:id/email"
          class="android.widget.EditText" package="com.example.app" content-desc=""
          clickable="true" enabled="true" bounds="[100,600][900,700]" />
    <node index="2" text="Password" resource-id="com.example.app:id/pw"
          class="android.widget.EditText" package="com.example.app" content-desc=""
          clickable="true" enabled="true" password="true" bounds="[100,800][900,900]" />
    <node index="3" text="Cancel" resource-id="com.example.app:id/cancel"
          class="android.widget.Button" package="com.example.app" content-desc=""
          clickable="true" enabled="false" bounds="[600,1000][900,1100]" />
    <node index="4" text="" resource-id="" class="android.widget.FrameLayout"
          package="com.example.app" content-desc="" clickable="true" enabled="true"
          bounds="[0,0][8,8]" />
    <node index="5" text="Gone" resource-id="com.example.app:id/gone"
          class="android.widget.Button" package="com.example.app" content-desc=""
          clickable="true" enabled="true" bounds="[0,0][0,0]" />
    <node index="6" text="" resource-id="com.example.app:id/list"
          class="androidx.recyclerview.widget.RecyclerView" package="com.example.app"
          content-desc="" clickable="false" scrollable="true" enabled="true"
          bounds="[0,1200][1080,2000]" />
  </node>
</hierarchy>
"""

SCREEN = (1080, 2160)


def observation(xml: str = DUMP, can_write: bool = True, foreground: str = "com.example.app/.Main") -> android.Observation:
    return android.Observation(
        serial="ABC123",
        nodes=android.parse_nodes(xml),
        screen=SCREEN,
        foreground=foreground,
        can_write=can_write,
    )


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# -- parsing ---------------------------------------------------------------


def test_parses_nodes_in_document_order_with_refs() -> None:
    nodes = android.parse_nodes(DUMP)
    assert [n.ref for n in nodes] == list(range(len(nodes)))
    assert nodes[1].text == "Sign in"
    assert nodes[1].cls == "android.widget.Button"


def test_parses_bounds_and_centre() -> None:
    sign_in = android.parse_nodes(DUMP)[1]
    assert sign_in.bounds == (100, 1000, 500, 1100)
    assert sign_in.centre == (300, 1050), "the tap point is the centre of the box"
    assert sign_in.width == 400
    assert sign_in.height == 100


def test_editable_class_is_recognised_however_it_is_packaged() -> None:
    """Apps subclass EditText constantly; matching the exact class name would
    miss most real text fields."""
    nodes = android.parse_nodes(
        """<hierarchy><node class="com.example.MyEditText" text="" package="p"
             clickable="true" bounds="[0,0][100,50]" /></hierarchy>"""
    )
    assert nodes[0].editable is True
    assert android.role_of(nodes[0]) == "text field"


def test_password_and_checked_are_read_but_never_echoed() -> None:
    nodes = android.parse_nodes(DUMP)
    password = nodes[3]
    assert password.password is True
    assert "masked" in android.describe(password)


def test_unparseable_xml_is_an_adb_error() -> None:
    with pytest.raises(android.AdbError):
        android.parse_nodes("not xml at all")


def test_node_without_bounds_is_skipped() -> None:
    nodes = android.parse_nodes(
        '<hierarchy><node class="android.widget.Button" text="x" clickable="true" /></hierarchy>'
    )
    assert nodes == []


# -- candidate gating ------------------------------------------------------


def test_disabled_and_zero_size_nodes_are_not_candidates() -> None:
    candidates = {c["description"] for c in observation().candidates}
    assert not any("Cancel" in c for c in candidates), "a disabled button is not a target"
    assert not any("Gone" in c for c in candidates), "a zero-size node cannot be tapped"


def test_a_tiny_unlabelled_container_is_not_a_candidate() -> None:
    """Invisible layout wrappers would otherwise swamp the option set with
    entries that read identically."""
    candidates = {c["id"] for c in observation().candidates}
    refs = {n.ref: n for n in android.parse_nodes(DUMP)}
    tiny = next(n for n, _ in [(n, None) for n in refs.values() if n.bounds == (0, 0, 8, 8)])
    assert str(tiny.ref) not in candidates


def test_a_large_unlabelled_container_is_a_candidate() -> None:
    """A big unlabelled clickable is a real target — a card or a tile."""
    xml = (
        '<hierarchy><node class="android.widget.FrameLayout" text="" package="p"'
        ' clickable="true" bounds="[0,900][1080,1800]" /></hierarchy>'
    )
    candidates = android.Observation("s", android.parse_nodes(xml), SCREEN).candidates
    assert len(candidates) == 1


def test_edittext_is_fillable_and_also_clickable() -> None:
    candidates = {c["label"]: c for c in observation().candidates}
    assert candidates["Email"]["fillable"] is True
    assert candidates["Email"]["clickable"] is True, "a field must be tappable to focus it"
    assert candidates["Sign in"]["fillable"] is False


def test_scrollable_container_is_offered() -> None:
    """A list is a legitimate target: tapping it focuses it, and its presence is
    what tells the model the screen has more below."""
    nodes = android.parse_nodes(DUMP)
    scrollable = next(n for n in nodes if n.scrollable)
    assert str(scrollable.ref) in {c["id"] for c in observation().candidates}


def test_duplicate_labels_get_a_positional_hint() -> None:
    """Jev's confidence measures concentration, so two candidates that read the
    same always look like doubt. Naming where each one is separates them."""
    xml = "".join(
        f'<node class="android.widget.Button" text="OK" package="p" clickable="true"'
        f' bounds="[0,{y}][200,{y + 80}]" />'
        for y in (0, 900, 2000)
    )
    candidates = android.Observation(
        "s", android.parse_nodes(f"<hierarchy>{xml}</hierarchy>"), SCREEN
    ).candidates
    descriptions = sorted(c["description"] for c in candidates)
    assert len(set(descriptions)) == 3, "each entry must be distinguishable"
    assert any("top" in d for d in descriptions)
    assert any("bottom" in d for d in descriptions)


def test_two_candidates_in_the_same_region_are_still_distinguishable() -> None:
    """Measured on a real lock screen: two stacked cards in the same region, both
    rendered as `framelayout - middle-centre`. Jev answered 0.37 — under the
    threshold — and the run refused to act. The ambiguity *was* the uncertainty:
    the model was not unsure about the goal, it could not tell which of two
    identical options it was choosing between."""
    xml = "".join(
        f'<node class="android.widget.FrameLayout" text="" package="p" clickable="true"'
        f' bounds="[100,{y}][900,{y + 200}]" />'
        for y in (900, 905)
    )
    candidates = android.Observation(
        "s", android.parse_nodes(f"<hierarchy>{xml}</hierarchy>"), SCREEN
    ).candidates
    descriptions = [c["description"] for c in candidates]
    assert len(candidates) == 2
    assert len(set(descriptions)) == 2, f"options must be mutually exclusive: {descriptions}"
    assert "at" in descriptions[0], "the tap point is the fallback that always separates"


def test_every_candidate_description_is_unique() -> None:
    descriptions = [c["description"] for c in observation().candidates]
    assert len(descriptions) == len(set(descriptions))


def test_a_row_label_is_harvested_from_a_non_clickable_child() -> None:
    """The most important thing here for real-world Android, and it was found by
    pointing the engine at a live Settings screen rather than at a fixture.

    A tappable row is a bare container; the words live in a child `TextView` that
    is not clickable. Without harvesting, every row on every Settings screen, chat
    list and inbox is offered as `linearlayout - middle-centre`, and the model is
    asked which of nineteen identical boxes opens Battery. Measured: Jev answered
    0.17 and refused — correctly, because the information was not in the option
    set."""
    xml = (
        '<hierarchy>'
        '<node class="android.widget.LinearLayout" text="" package="p" clickable="true"'
        ' bounds="[0,900][1080,1080]">'
        '<node class="android.widget.TextView" text="Battery" package="p"'
        ' clickable="false" bounds="[100,930][400,990]" />'
        '<node class="android.widget.TextView" text="Power saving mode" package="p"'
        ' clickable="false" bounds="[100,995][600,1050]" />'
        '</node>'
        '</hierarchy>'
    )
    node = android.parse_nodes(xml)[0]
    assert node.derived == "Battery, Power saving mode"
    assert node.label() == "Battery, Power saving mode"
    assert '"Battery, Power saving mode"' in android.describe(node)


def test_a_container_does_not_absorb_a_clickable_childs_label() -> None:
    """Otherwise every wrapper becomes a duplicate of the row inside it and the
    option set fills with near-identical entries."""
    xml = (
        '<hierarchy>'
        '<node class="android.widget.FrameLayout" text="" package="p" clickable="true"'
        ' bounds="[0,0][1080,2000]">'
        '<node class="android.widget.Button" text="Sign in" package="p"'
        ' clickable="true" bounds="[100,1000][400,1100]" />'
        '</node>'
        '</hierarchy>'
    )
    nodes = android.parse_nodes(xml)
    assert nodes[0].derived == "", "a clickable child's label belongs to the child"


def test_rows_on_a_settings_like_screen_become_legible() -> None:
    """End to end: the harvested labels must survive into the option set."""
    rows = [
        ("Battery", "Power saving mode"),
        ("Display", "Eye comfort"),
        ("Sound", "Do not disturb"),
    ]
    xml = "<hierarchy>" + "".join(
        f'<node class="android.widget.LinearLayout" text="" package="com.android.settings"'
        f' clickable="true" bounds="[0,{200 + i * 160}][1080,{340 + i * 160}]">'
        f'<node class="android.widget.TextView" text="{title}" package="com.android.settings"'
        f' clickable="false" bounds="[40,{220 + i * 160}][400,{280 + i * 160}]" />'
        f'<node class="android.widget.TextView" text="{summary}" package="com.android.settings"'
        f' clickable="false" bounds="[40,{285 + i * 160}][700,{320 + i * 160}]" />'
        f"</node>"
        for i, (title, summary) in enumerate(rows)
    ) + "</hierarchy>"

    candidates = android.Observation(
        "s", android.parse_nodes(xml), SCREEN
    ).candidates
    descriptions = [c["description"] for c in candidates]
    assert any("Battery" in d for d in descriptions), descriptions
    assert any("Display" in d for d in descriptions), descriptions
    assert len(set(descriptions)) == len(descriptions)


def test_pixel_identical_candidates_collapse_to_one() -> None:
    """Identical bounds means identical tap coordinates, so they are not two
    options — they are the same action offered twice. Nested clickable containers
    produce exactly this, and the model then spends confidence on a distinction
    that does not exist."""
    xml = (
        '<hierarchy>'
        '<node class="android.widget.FrameLayout" text="" package="p" clickable="true"'
        ' bounds="[10,200][1070,280]">'
        '<node class="android.widget.LinearLayout" text="" package="p" clickable="true"'
        ' bounds="[10,200][1070,280]" />'
        '<node class="android.widget.LinearLayout" text="" package="p" clickable="true"'
        ' bounds="[10,200][1070,280]" />'
        '</node>'
        '</hierarchy>'
    )
    candidates = android.Observation(
        "s", android.parse_nodes(xml), SCREEN
    ).candidates
    assert len(candidates) == 1
    assert len({c["centred"] for c in candidates}) == 1


def test_a_labelled_twin_wins_the_dedupe() -> None:
    xml = (
        '<hierarchy>'
        '<node class="android.widget.FrameLayout" text="" package="p" clickable="true"'
        ' bounds="[10,200][1070,280]">'
        '<node class="android.widget.TextView" text="Wi-Fi" package="p" clickable="false"'
        ' bounds="[40,220][200,260]" />'
        '</node>'
        '<node class="android.widget.LinearLayout" text="" package="p" clickable="true"'
        ' bounds="[10,200][1070,280]" />'
        '</hierarchy>'
    )
    candidates = android.Observation(
        "s", android.parse_nodes(xml), SCREEN
    ).candidates
    assert len(candidates) == 1
    assert "Wi-Fi" in candidates[0]["description"], "the legible one is kept"


def test_identical_bounds_still_leave_unique_descriptions() -> None:
    """The ref fallback is the guarantee: identical bounds defeat the position
    hint *and* the tap point, and the option set still has to be separable."""
    nodes = [
        android.Node(
            ref=i, cls="android.widget.FrameLayout", package="p", text="", content_desc="",
            resource_id="", bounds=(0, 0, 100, 100), clickable=True, scrollable=False,
            long_clickable=False, checkable=False, checked=False, enabled=True,
            password=False,
        )
        for i in range(3)
    ]
    descriptions = android.unique_descriptions(nodes, SCREEN)
    assert len(set(descriptions)) == 3
    assert all("element" in d for d in descriptions)


# -- navigation ------------------------------------------------------------


@pytest.mark.parametrize(
    "goal, expected",
    [
        ("open https://whoer.net", "https://whoer.net"),
        ("go on whoer.net and show its details", "https://whoer.net"),
        ("check whatismyipaddress.com", "https://whatismyipaddress.com"),
        ("open http://example.com/path?q=1", "http://example.com/path?q=1"),
        ("open github.com/anthropics", "https://github.com/anthropics"),
    ],
)
def test_a_url_is_pulled_out_of_the_goal(goal: str, expected: str) -> None:
    assert android.url_in(goal) == expected


@pytest.mark.parametrize(
    "goal",
    [
        "open the Settings app",              # no dotted token at all
        "set the version to 3.36",            # a numeric TLD is not a domain
        "open com.whatsapp",                  # a package name is not a destination
        "scroll down a bit",
        "",
    ],
)
def test_a_goal_without_a_real_url_offers_no_navigation(goal: str) -> None:
    """Requiring a known TLD is what keeps `com.whatsapp` and `3.36` from being
    read as destinations — either would send the phone somewhere absurd."""
    assert android.url_in(goal) is None


def test_navigate_is_offered_only_when_the_goal_names_a_url() -> None:
    plain = android.Observation("s", android.parse_nodes(DUMP), SCREEN)
    assert "navigate" not in plain.operations()

    with_url = android.Observation(
        "s", android.parse_nodes(DUMP), SCREEN, navigate_url="https://whoer.net"
    )
    assert "navigate" in with_url.operations()


def test_navigate_is_executed_as_an_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Intent, not address-bar driving: no text entry needed, works from any
    screen, one adb call. The URL is quoted, or `&` in a query string would be
    read as a command separator."""
    seen: list[str] = []
    monkeypatch.setattr(android, "shell", lambda _s, command: seen.append(command) or "")

    android.open_url("S", "https://example.com/a?x=1&y=2")

    assert len(seen) == 1
    assert seen[0].startswith("am start -a android.intent.action.VIEW -d ")
    assert "'https://example.com/a?x=1&y=2'" in seen[0]


def test_navigate_needs_a_url_it_was_promised() -> None:
    """Fail closed rather than opening something unexpected."""
    observation = android.Observation("s", android.parse_nodes(DUMP), SCREEN)
    with pytest.raises(android.AdbError):
        android.execute("S", Decision(kind="navigate"), observation)


def test_navigate_survives_the_plan_cache() -> None:
    observation = android.Observation(
        "s", android.parse_nodes(DUMP), SCREEN, navigate_url="https://whoer.net"
    )
    step = android.Step(0, Decision(kind="navigate"), observation, True)
    assert android.plan_from(android.Result(goal="g", steps=[step])) == [
        {"kind": "navigate", "target": ""}
    ]


# -- speed ---------------------------------------------------------------


def test_screen_size_is_memoised_per_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wm size` measured at 800 ms on a real device — over a quarter of the old
    per-snapshot budget, for a value that does not change between two screens."""
    android.forget_screen_size()
    calls: list[int] = []
    monkeypatch.setattr(
        android, "shell", lambda *a, **k: calls.append(1) or "Physical size: 1080x2160"
    )

    assert android.cached_screen_size("S", "0") == (1080, 2160)
    assert android.cached_screen_size("S", "0") == (1080, 2160)
    assert len(calls) == 1, "one `wm size` per orientation, not one per step"

    assert android.cached_screen_size("S", "1") == (1080, 2160)
    assert len(calls) == 2, "a rotation must invalidate it, or a rotated device gets stale dimensions"

    android.forget_screen_size("S")
    android.cached_screen_size("S", "1")
    assert len(calls) == 3

    android.forget_screen_size()
    assert android._SCREEN_CACHE == {}


def test_rotation_is_read_from_the_dump_for_free() -> None:
    assert android.rotation_of("<hierarchy rotation=\"0\">") == "0"
    assert android.rotation_of('<hierarchy rotation="3">') == "3"
    assert android.rotation_of("<hierarchy>") == ""


def test_snapshot_does_not_re_query_the_screen_size(monkeypatch: pytest.MonkeyPatch) -> None:
    android.forget_screen_size()
    sizes: list[int] = []
    monkeypatch.setattr(android, "dump", lambda *a, **k: DUMP)
    monkeypatch.setattr(android, "foreground", lambda *a, **k: "app/.Main")
    monkeypatch.setattr(
        android, "shell", lambda *a, **k: sizes.append(1) or "Physical size: 1080x2160"
    )

    android.snapshot("S")
    android.snapshot("S")
    android.snapshot("S")
    assert len(sizes) == 1, "three steps must not cost three `wm size` calls"


def test_the_settle_poll_never_takes_a_full_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poll only asks "has the foreground app changed?" — answering that with
    a ~2.9 s hierarchy dump was most of a step's budget, spent to read one string."""
    snapshots: list[int] = []
    monkeypatch.setattr(
        android, "snapshot", lambda *a, **k: snapshots.append(1) or observation()
    )
    monkeypatch.setattr(android, "tap", lambda *a, **k: None)
    monkeypatch.setattr(android, "foreground", lambda *a, **k: "app/.Main")
    monkeypatch.setattr(android, "focus_signature", lambda *a, **k: "app/.Main|win")

    class Chooser:
        def choose(self, *_a: Any) -> Decision:
            return Decision(kind="click_element", element_id="1", confidence=0.99)

    # settle=0.5 means the poll spins several times; none of them may dump.
    android.run("S", "g", Chooser(), act=True, max_steps=3, settle=0.5)
    assert len(snapshots) == 3, "exactly one snapshot per step, none from the poll"


def test_focus_signature_reads_app_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `dumpsys window` yields both the app and the focused window, so an
    in-app change (a dialog, a tab) is visible for the same ~52 ms cost."""
    text = (
        "mFocusedApp=ActivityRecord{abc u0 com.example.app/.Main t42}\n"
        "mCurrentFocus=Window{9f8e7d u0 com.example.app/com.example.app.Main}\n"
    )
    monkeypatch.setattr(android, "shell", lambda *a, **k: text)
    assert android.focus_signature("S") == "com.example.app/.Main|com.example.app/com.example.app.Main"


def test_focus_signature_only_moves_with_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honest about the limit. A new focused window (a dialog) is visible, but a
    tab or fragment swap inside one activity keeps the same window and is NOT —
    that case is guarded at replay time by description resolution, not here."""
    same_app = "mFocusedApp=ActivityRecord{abc u0 com.example.app/.Main t42}"
    same_window = same_app + "\nmCurrentFocus=Window{1 u0 com.example.app.Main}"
    dialog = same_app + "\nmCurrentFocus=Window{2 u0 com.example.app.Dialog}"

    monkeypatch.setattr(android, "shell", lambda *a, **k: same_window)
    assert android.focus_signature("S") == "com.example.app/.Main|com.example.app.Main"
    monkeypatch.setattr(android, "shell", lambda *a, **k: dialog)
    assert android.focus_signature("S") != "com.example.app/.Main|com.example.app.Main"


def test_android_defaults_are_single_sourced() -> None:
    """The schema and the handler must not be able to drift apart — they did once
    in the browser surface and silently changed behaviour."""
    from jev_use import mcp_server

    schema = {t["name"]: t for t in mcp_server.TOOLS}["android_use"]["inputSchema"]["properties"]
    assert schema["max_steps"]["default"] == mcp_server.DEFAULT_MAX_STEPS
    assert schema["min_confidence"]["default"] == mcp_server.DEFAULT_MIN_CONFIDENCE
    assert schema["settle"]["default"] == android.DEFAULT_SETTLE


def test_android_settles_faster_than_the_browser() -> None:
    """Measured, not taste: on Android most actions navigate *within* an app, so
    the foreground never changes and the poll cannot fire — a long settle is dead
    time. The dump blocks on UI idle itself (3.0-3.4 s in a transition vs 2.1 s
    still), so this is a floor guard rather than the settling mechanism."""
    assert android.DEFAULT_SETTLE < 3.0, "the browser's 3 s is too long here"


def test_a_decomposition_dump_is_reused_as_the_first_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner's dump WAS thrown away and re-taken — ~2.1 s per decomposed run.
    It seeds the first step now."""
    snapshots: list[int] = []
    monkeypatch.setattr(
        android, "snapshot", lambda *a, **k: snapshots.append(1) or observation()
    )
    monkeypatch.setattr(android, "dump", lambda *a, **k: DUMP)
    monkeypatch.setattr(android, "foreground", lambda *a, **k: "app/.Main")

    class Writer:
        available = True

        def decompose(self, goal: str, summary: str) -> list[str]:
            return [goal]  # one subgoal: the <2 branch that used to double-dump

        def write(self, *a: Any, **k: Any) -> str:
            return "x"

    class Chooser:
        def choose(self, *_a: Any) -> Decision:
            return Decision(kind="done")

    result = android.run("S", "g", Chooser(), act=False, writer=Writer())
    assert result.outcome == "done"
    assert len(snapshots) == 1, "the planner's dump must seed the first step"


def test_focus_signature_handles_work_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A work profile is user 10; a hardcoded `u0` read it as no focused window."""
    text = (
        "mFocusedApp=ActivityRecord{abc u10 com.example.app/.Main t42}\n"
        "mCurrentFocus=Window{9f8e7d u10 com.example.app/com.example.app.Main}\n"
    )
    monkeypatch.setattr(android, "shell", lambda *a, **k: text)
    assert android.focus_signature("S") == "com.example.app/.Main|com.example.app/com.example.app.Main"


def test_focus_signature_is_unknown_when_adb_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable device must not look like a screen transition."""

    def boom(*_a: Any, **_k: Any) -> str:
        raise android.AdbError("device offline")

    monkeypatch.setattr(android, "shell", boom)
    assert android.focus_signature("S") == ""
    assert android._screen_moved("", "app/.Main") is False, "unavailable is not a move"
    assert android._screen_moved("app/.Main", "") is False
    assert android._screen_moved("app/.Main", "app/.Main") is False
    assert android._screen_moved("app/.Main", "app/Other") is True


def test_android_replay_refuses_an_all_targetless_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """back/home/scroll/navigate have no target to validate against the screen, so a
    plan made only of them could run on the wrong screen inside the same activity."""
    snapshots: list[int] = []
    monkeypatch.setattr(
        android, "snapshot", lambda *a, **k: snapshots.append(1) or observation()
    )
    monkeypatch.setattr(android, "key", lambda *a, **k: None)

    result = android.replay("S", "g", [{"kind": "go_back", "target": ""}], act=True, settle=0.1)
    assert result.outcome == "replay_miss"
    assert snapshots == [], "it must refuse before dumping the hierarchy"


def test_android_replay_refuses_a_plan_that_starts_targetless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`go_back -> click "Settings"` would go back on the wrong screen before
    "Settings" was ever resolved, so the FIRST action must be a described target."""
    monkeypatch.setattr(
        android, "snapshot", lambda *a, **k: pytest.fail("must refuse before dumping")
    )
    plan = [
        {"kind": "go_back", "target": ""},
        {"kind": "click_element", "target": "Settings"},
    ]
    result = android.replay("S", "g", plan, act=True, settle=0.1)
    assert result.outcome == "replay_miss"


def test_focus_signature_is_unknown_when_the_dump_is_unrecognised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parsed-but-unrecognised dump must not yield the truthy key "|"."""
    monkeypatch.setattr(android, "shell", lambda *a, **k: "nothing about windows here")
    assert android.focus_signature("S") == ""


def test_android_replay_refuses_an_ambiguous_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = observation()
    wanted = page.candidates[0]["description"]
    page._candidates.append(dict(page._candidates[0], id="999"))
    monkeypatch.setattr(android, "snapshot", lambda *a, **k: page)
    monkeypatch.setattr(android, "tap", lambda *a, **k: None)

    result = android.replay(
        "S", "g", [{"kind": "click_element", "target": wanted}], act=True, settle=0.1
    )
    assert result.outcome == "replay_miss"
    assert "2 elements match" in result.steps[0].note


def test_android_cache_key_uses_the_window_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key includes the window signature, not just the foreground app."""
    from jev_use.cache import PlanCache

    page = observation()
    clickable = next(c for c in page.candidates if c["clickable"])
    monkeypatch.setattr(android, "focus_signature", lambda *a, **k: "app/.Main|tabA")
    monkeypatch.setattr(android, "snapshot", lambda *a, **k: page)
    monkeypatch.setattr(android, "tap", lambda *a, **k: None)

    cache = PlanCache(tmp_path / "c.json")
    # The engine keys on f"android|<signature>|{goal}" and PlanCache appends "::{goal}".
    cache.put(
        "android|app/.Main|tabA|g", "g",
        [{"kind": "click_element", "target": clickable["description"]}],
    )
    # The old app-only key must not be what gets replayed.
    cache.put("android|app/.Main|g", "g", [{"kind": "click_element", "target": "nope"}])

    class Chooser:
        def choose(self, *_a: Any) -> Decision:
            pytest.fail("a cache hit must not consult the model")

    result = android.run("S", "g", Chooser(), act=True, cache=cache, max_steps=2, settle=0.1)
    assert result.outcome == "replayed"


def test_android_replay_skips_the_final_dump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single-action replay used to dump the hierarchy again after acting, with
    nothing left to read it — ~2.1 s of pure waste."""
    from jev_use.cache import PlanCache

    page = observation()
    clickable = next(c for c in page.candidates if c["clickable"])
    snapshots: list[int] = []
    monkeypatch.setattr(android, "focus_signature", lambda *a, **k: "app/.Main|tabA")
    monkeypatch.setattr(android, "snapshot", lambda *a, **k: snapshots.append(1) or page)
    monkeypatch.setattr(android, "tap", lambda *a, **k: None)

    cache = PlanCache(tmp_path / "c.json")
    cache.put(
        "android|app/.Main|tabA|g", "g",
        [{"kind": "click_element", "target": clickable["description"]}],
    )

    class Chooser:
        def choose(self, *_a: Any) -> Decision:
            pytest.fail("a cache hit must not consult the model")

    result = android.run("S", "g", Chooser(), act=True, cache=cache, max_steps=1, settle=0.1)
    assert result.outcome == "replayed"
    assert len(snapshots) == 1, "only the initial dump; the final one is skipped"


def test_deterministic_decision_handles_back_and_scroll() -> None:
    obs = observation()
    back = android.deterministic_decision("go back", obs)
    assert back is not None and back.kind == "go_back"
    scroll = android.deterministic_decision("scroll down", obs)
    assert scroll is not None and scroll.kind == "scroll_down"
    assert android.deterministic_decision("open the settings app", obs) is None


def test_exact_home_tab_tap_outranks_android_system_home_alias() -> None:
    obs = observation('''<hierarchy>
      <node class="android.widget.TextView" text="Home, tab 1 of 6"
      content-desc="Home, tab 1 of 6" clickable="true" package="com.facebook.katana"
      bounds="[0,1920][180,2060]" />
      <node class="android.widget.TextView" text="Menu, tab 6 of 6"
      content-desc="Menu, tab 6 of 6" clickable="true" package="com.facebook.katana"
      bounds="[900,1920][1080,2060]" />
    </hierarchy>''')
    home_tab = next(target for target in obs.targets
                    if target["label"] == "Home, tab 1 of 6")

    exact_tap = android.deterministic_decision("tap " + home_tab["description"], obs)
    assert exact_tap is not None
    assert exact_tap.kind == "click_element"
    assert exact_tap.element_id == home_tab["id"]
    assert exact_tap.source == "deterministic"

    system_home = android.deterministic_decision("go home", obs)
    assert system_home is not None
    assert system_home.kind == "go_home"


def test_ambiguous_exact_android_target_fails_closed() -> None:
    obs = observation('''<hierarchy>
      <node class="android.widget.Button" text="Home" clickable="true"
      bounds="[0,100][300,200]" />
      <node class="android.widget.Button" text="Home" clickable="true"
      bounds="[300,100][600,200]" />
    </hierarchy>''')
    decision = android.deterministic_decision("tap Home", obs)
    assert decision is not None
    assert decision.kind == "click_element"
    assert decision.rejection == "the exact Android target is ambiguous"


def test_explicit_tap_of_unobserved_control_never_falls_through_to_navigation() -> None:
    obs = observation()
    for goal in ("tap What's on your mind?", "tap Home, tab 1 of 6"):
        decision = android.deterministic_decision(goal, obs)
        assert decision is not None
        assert decision.kind == "click_element"
        assert decision.element_id is None
        assert "no exact Android target" in (decision.rejection or "")


def test_exact_description_selects_unlabelled_control_without_guessing() -> None:
    from jev_use.choosers import deterministic_decision
    obs = observation('''<hierarchy>
      <node class="android.view.View" clickable="true" resource-id="app:id/nav"
      bounds="[720,1920][900,2060]" />
      <node class="android.view.View" clickable="true" resource-id="app:id/nav"
      bounds="[900,1920][1080,2060]" />
    </hierarchy>''')
    assert deterministic_decision('tap view', obs) is None
    assert deterministic_decision('open the menu', obs) is None
    target = obs.targets[1]
    decision = deterministic_decision('tap ' + target['description'], obs)
    assert decision.element_id == target['id']
    assert decision.confidence == 1.0
    assert decision.source == 'deterministic'
    obs.targets[0]['description'] = target['description']
    assert deterministic_decision('tap ' + target['description'], obs) is None


def test_deterministic_decision_leaves_compound_goals_to_jev() -> None:
    """A prefix match would run the first clause and drop the rest."""
    from jev_use.choosers import deterministic_decision

    obs = observation()
    for goal in ("go back and open settings", "scroll down then tap Checkout", "go home after saving"):
        assert deterministic_decision(goal, obs) is None, goal


def test_password_is_masked_in_the_description() -> None:
    descriptions = [c["description"] for c in observation().candidates]
    assert any("(masked)" in d for d in descriptions)


# -- operations ------------------------------------------------------------


def test_click_is_withheld_when_nothing_can_be_tapped() -> None:
    empty = android.Observation(
        "s", android.parse_nodes("<hierarchy/>"), SCREEN
    )
    assert "click_element" not in empty.operations()


def test_type_text_is_withheld_without_a_writer() -> None:
    assert "type_text" not in observation(can_write=False).operations()
    assert "type_text" in observation(can_write=True).operations()


def test_type_text_is_withheld_when_there_is_nowhere_to_type() -> None:
    xml = (
        '<hierarchy><node class="android.widget.Button" text="OK" package="p"'
        ' clickable="true" bounds="[0,0][200,80]" /></hierarchy>'
    )
    observation_without_fields = android.Observation(
        "s", android.parse_nodes(xml), SCREEN, can_write=True
    )
    assert "type_text" not in observation_without_fields.operations()


def test_navigation_operations_are_always_offered() -> None:
    """Back is the most useful action on Android: more of the UI is reachable by
    leaving a screen than by anything on it."""
    operations = observation().operations()
    assert "go_back" in operations
    assert "go_home" in operations


def test_text_target_head_contains_only_fillable_fields() -> None:
    heads = observation().target_heads()
    labels = {c["id"]: c["label"] for c in observation().candidates}
    assert {labels[i] for i in heads["text_target"]} == {"Email", "[password field]"}
    assert any(labels[i] == "Sign in" for i in heads["click_target"])


def test_targets_for_respects_the_operation() -> None:
    obs = observation()
    assert {c["label"] for c in obs.targets_for("type_text")} == {"Email", "[password field]"}
    assert any(c["label"] == "Sign in" for c in obs.targets_for("click_element"))
    assert obs.targets_for("go_back") == []


def test_validate_refuses_a_field_chosen_for_a_click() -> None:
    obs = observation()
    field = next(c["id"] for c in obs.candidates if c["fillable"])
    decision = android.validate(Decision(kind="click_element", element_id=field), obs)
    # A field IS clickable, so this is legal; the illegal case is a text target
    # that is not fillable.
    assert decision.accepted

    label = next(c["id"] for c in obs.candidates if not c["fillable"])
    decision = android.validate(Decision(kind="type_text", element_id=label), obs)
    assert not decision.accepted, "a non-fillable element is not a legal type target"


def test_validate_refuses_an_operation_the_screen_does_not_offer() -> None:
    obs = observation(can_write=False)
    decision = android.validate(
        Decision(kind="type_text", element_id=obs.candidates[0]["id"]), obs
    )
    assert not decision.accepted
    assert "not offered" in decision.rejection


# -- readouts and reading --------------------------------------------------


def test_readouts_lead_with_the_app() -> None:
    assert observation().readouts()[0] == "app: com.example.app/.Main"


def test_readouts_never_leak_a_password_value() -> None:
    xml = (
        '<hierarchy><node class="android.widget.EditText" text="hunter2" package="p"'
        ' password="true" clickable="true" bounds="[0,0][400,100]" /></hierarchy>'
    )
    obs = android.Observation("s", android.parse_nodes(xml), SCREEN)
    assert "hunter2" not in " ".join(obs.readouts())
    assert "hunter2" not in obs.read()
    assert "[password field]" in obs.read()
    assert 'hunter2' not in str(obs.candidates)


def test_read_joins_the_visible_text() -> None:
    text = observation().read()
    assert "Sign in" in text
    assert "Email" in text


# -- text entry ------------------------------------------------------------


def test_spaces_become_the_escape_input_understands() -> None:
    assert android.escape_text("hello world") == "hello%sworld"


def test_a_value_is_single_quoted_for_the_device_shell() -> None:
    assert android.shell_quote("hello world") == "'hello world'"


def test_an_embedded_single_quote_is_escaped_not_dropped() -> None:
    """Unescaped, one apostrophe ends the quoted string and the rest of the value
    is executed by the device shell."""
    assert android.shell_quote("it's") == "'it'\\''s'"


def test_non_ascii_is_refused_rather_than_mangled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`input text` cannot deliver it, and typing a corrupted value into a field
    is worse than declining."""

    def explode(*_a: Any, **_k: Any) -> str:
        raise AssertionError("must refuse before touching the device")

    monkeypatch.setattr(android, "shell", explode)
    with pytest.raises(android.AdbError, match="non-ASCII"):
        android.type_into("ABC", "naïve")


def test_ascii_text_is_quoted_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(android, "shell", lambda _s, command: seen.append(command) or "")
    android.type_into("ABC", "hi there")
    assert seen == ["input text 'hi%sthere'"]


# -- device selection ------------------------------------------------------


def fake_devices(monkeypatch: pytest.MonkeyPatch, table: str) -> None:
    monkeypatch.setattr(android, "adb", lambda *a, **k: FakeCompleted(stdout=table))


def test_parses_the_device_table(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_devices(
        monkeypatch,
        "List of devices attached\n"
        "FFY5T18202024660       device usb:1-5 product:RNE-L21 model:RNE_L21 transport_id:1\n",
    )
    found = android.devices()
    assert len(found) == 1
    assert found[0].serial == "FFY5T18202024660"
    assert found[0].model == "RNE_L21"
    assert found[0].usable is True


def test_unauthorized_device_is_reported_with_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The most common failure, and invisible if unusable devices are filtered out."""
    fake_devices(monkeypatch, "List of devices attached\nABC123\tunauthorized\n")
    assert android.devices()[0].state == "unauthorized"
    with pytest.raises(android.AdbError, match="Allow USB debugging"):
        android.pick_device()


def test_no_device_names_the_two_ways_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_devices(monkeypatch, "List of devices attached\n")
    with pytest.raises(android.AdbError, match="USB debugging"):
        android.pick_device()


def test_two_devices_requires_a_choice_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving the wrong phone answers every question wrongly, which is worse
    than refusing."""
    fake_devices(
        monkeypatch,
        "List of devices attached\nAAA\tdevice model:One\nBBB\tdevice model:Two\n",
    )
    with pytest.raises(android.AdbError, match="more than one device"):
        android.pick_device()
    assert android.pick_device("BBB").serial == "BBB"


def test_an_unknown_serial_lists_what_is_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_devices(monkeypatch, "List of devices attached\nAAA\tdevice model:One\n")
    with pytest.raises(android.AdbError, match="AAA"):
        android.pick_device("NOPE")


def test_screen_size_prefers_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        android, "shell", lambda *a, **k: "Physical size: 1080x2160\nOverride size: 720x1440\n"
    )
    assert android.screen_size("S") == (720, 1440)


def test_screen_size_falls_back_to_physical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(android, "shell", lambda *a, **k: "Physical size: 1080x2160\n")
    assert android.screen_size("S") == (1080, 2160)


def test_foreground_reads_the_focused_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        android,
        "shell",
        lambda *a, **k: "mFocusedApp=AppWindowToken{x token=Token{y ActivityRecord{"
        "z u0 com.whatsapp/.HomeActivity t1}}",
    )
    assert android.foreground("S") == "com.whatsapp/.HomeActivity"


# -- the loop --------------------------------------------------------------


def test_plan_stores_descriptions_never_refs() -> None:
    obs = observation()
    target = next(c for c in obs.candidates if c["label"] == "Sign in")
    step = android.Step(
        0, Decision(kind="click_element", element_id=target["id"]), obs, True
    )
    plan = android.plan_from(android.Result(goal="g", steps=[step]))
    assert plan == [{"kind": "click_element", "target": 'button "Sign in"'}]
    assert target["id"] not in str(plan)


def test_plan_records_navigation_without_a_target() -> None:
    obs = observation()
    step = android.Step(0, Decision(kind="go_back"), obs, True)
    assert android.plan_from(android.Result(goal="g", steps=[step])) == [
        {"kind": "go_back", "target": ""}
    ]


def test_replay_aborts_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        android, "snapshot", lambda *a, **k: observation(foreground="")
    )
    result = android.replay(
        "S", "g", [{"kind": "click_element", "target": 'button "Nope"'}], act=True
    )
    assert result.outcome == "replay_miss"
    assert "nothing described" in result.steps[0].note


def test_replay_finds_an_element_by_description(monkeypatch: pytest.MonkeyPatch) -> None:
    taps: list[tuple[int, int]] = []
    monkeypatch.setattr(android, "snapshot", lambda *a, **k: observation())
    monkeypatch.setattr(android, "tap", lambda _s, x, y: taps.append((x, y)))

    result = android.replay(
        "S", "g", [{"kind": "click_element", "target": 'button "Sign in"'}], act=True
    )
    assert result.outcome == "replayed"
    assert taps == [(300, 1050)], "the tap lands on the element's centre"


def test_scroll_down_swipes_up() -> None:
    """`scroll_down` means "reveal what is below", which is an upward swipe.
    Getting this backwards is the classic bug here."""
    swipes: list[tuple[int, int, int, int]] = []
    monkeypatched_swipe = lambda _s, x1, y1, x2, y2, *_a, **_k: swipes.append((x1, y1, x2, y2))
    original = android.swipe
    android.swipe = monkeypatched_swipe  # type: ignore[assignment]
    try:
        android.scroll("S", SCREEN, "scroll_down")
        android.scroll("S", SCREEN, "scroll_up")
    finally:
        android.swipe = original  # type: ignore[assignment]

    assert swipes[0][1] > swipes[0][3], "scroll_down moves the finger upward"
    assert swipes[1][1] < swipes[1][3], "scroll_up moves it downward"


def test_act_false_decides_without_touching_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`act=false` is the dry run, and it is what makes this safe to try on a
    real phone: nothing is dispatched."""

    def explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("act=false must not dispatch anything")

    monkeypatch.setattr(android, "snapshot", lambda *a, **k: observation())
    monkeypatch.setattr(android, "tap", explode)
    monkeypatch.setattr(android, "key", explode)
    monkeypatch.setattr(android, "swipe", explode)

    class Chooser:
        def choose(self, *_a: Any) -> Decision:
            return Decision(kind="click_element", element_id="1", confidence=0.99)

    result = android.run("S", "g", Chooser(), act=False, max_steps=1)
    assert result.actions == 0
    assert result.steps[0].note.startswith("would:")


# -- facebook account location ---------------------------------------------

def test_restart_facebook_force_stops_and_relaunches_without_clearing_data(monkeypatch):
    commands = []
    monkeypatch.setattr(android, "shell", lambda serial, command, **kwargs: commands.append((serial, command)) or (
        "priority=0\ncom.facebook.katana/.MainActivity\n" if "resolve-activity" in command else ""))
    android.restart_facebook("S")
    assert commands == [("S", "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.facebook.katana"),
                        ("S", "am force-stop com.facebook.katana"),
                        ("S", "am start -W -n com.facebook.katana/.MainActivity")]


LOCATION_PAGE = (
    "Your Primary Location\n"
    "Your primary location is near:\n"
    "Lahore, Punjab 54\n"
    "Primary location is determined by information we use to support Facebook "
    "Products, such as:\n"
    "The current city that you entered on your profile\n"
)

SIGN_IN_PAGE = "Log into Facebook\nForgotten password?\nCreate new account"


def test_the_location_is_the_line_under_the_label() -> None:
    """The page prints the label and the value on separate lines, which is why
    this reads the next non-empty line rather than matching one pattern."""
    assert android.parse_primary_location(LOCATION_PAGE) == "Lahore, Punjab 54"


def test_a_location_written_on_the_label_line_is_read_too() -> None:
    assert (
        android.parse_primary_location(
            "Your primary location is near: Sheikhupura, Punjab 39"
        )
        == "Sheikhupura, Punjab 39"
    )


def test_a_page_without_the_label_yields_nothing() -> None:
    """`None` is what lets the poll tell "not this page" from "this page says X"."""
    assert android.parse_primary_location(SIGN_IN_PAGE) is None
    assert android.parse_primary_location("") is None


@pytest.mark.parametrize('following', ['Primary location is determined by information we use', 'Learn more', 'Back', 'Loading…'])
def test_location_explanation_or_navigation_is_not_a_location(following):
    assert android.parse_primary_location('Your primary location is near:\n' + following) is None


def test_a_sign_in_page_is_recognised() -> None:
    assert android.is_signed_out(SIGN_IN_PAGE) is True
    assert android.is_signed_out(LOCATION_PAGE) is False


def test_the_webview_link_encodes_the_page_url_in_full() -> None:
    """The page URL's own `?` and `&` would otherwise be read as part of the deep
    link rather than as part of the page, and the route would open truncated."""
    link = android.webview_link(android.FACEBOOK_PRIMARY_LOCATION_URL)
    assert link.startswith("fb://facewebmodal/f?href=")

    encoded = link.split("href=", 1)[1]
    assert "?" not in encoded and "&" not in encoded, "the page URL's query must be encoded"
    assert "%3Fref%3Dbookmarks" in encoded


def test_each_check_asks_for_a_different_url() -> None:
    """Measured need: the in-app webview keeps the last page, so a re-run after
    switching accounts can render the previous account's answer. A changing query
    parameter is what makes the result follow the session rather than the cache."""
    first = android.cache_busted(android.FACEBOOK_PRIMARY_LOCATION_URL)
    second = android.cache_busted(android.FACEBOOK_PRIMARY_LOCATION_URL)
    assert first != second
    assert first.startswith(android.FACEBOOK_PRIMARY_LOCATION_URL)
    assert "&_jev=" in first, "the page URL already has a query, so this appends"
    assert android.cache_busted("https://x/info", stamp=7).endswith("?_jev=7")


def test_two_accounts_yield_their_own_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer follows the signed-in session, not the phone: two checks against
    one device in two accounts must report two locations, and must not be handed
    the same (cached) URL."""
    opened: list[str] = []
    pages = iter(
        [
            "Your primary location is near:\nLahore, Punjab 54",
            "Your primary location is near:\nCebu City, Central Visayas",
        ]
    )
    monkeypatch.setattr(android, "open_facebook_page", lambda _s, url, **kwargs: opened.append(url))
    monkeypatch.setattr(android, "screen_text", lambda _s: next(pages))

    first = android.account_location("S")
    second = android.account_location("S")

    assert first.location == "Lahore, Punjab 54"
    assert second.location == "Cebu City, Central Visayas"
    assert first.state == second.state == "location"
    assert opened[0] != opened[1], "the second check must not ask for the first's URL"


def test_zero_timeout_does_not_open_or_read_a_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two different fixes: "not signed in" needs a login, "did not render" needs
    another attempt. Collapsing both into an empty answer hides which it is."""
    monkeypatch.setattr(android, "open_facebook_page", lambda _s, url, **kwargs: None)
    monkeypatch.setattr(android, "screen_text", lambda _s: pytest.fail("expired deadline must not read"))

    found = android.account_location("S", timeout=0.0)

    assert found.location == ""
    assert found.state == "unknown"
    assert "had not rendered" in found.describe()


def test_a_page_that_never_renders_gives_up_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank webview must not hang the caller."""
    monkeypatch.setattr(android, "open_facebook_page", lambda _s, url, **kwargs: None)
    monkeypatch.setattr(android, "screen_text", lambda _s: "")

    found = android.account_location("S", timeout=0.0)

    assert found.location == ""
    assert found.state == "unknown"
    assert found.seconds >= 0.0


@pytest.mark.parametrize('text,state', [('Connection lost\nTap to retry\nWebpage not available', 'network_error'),
                                       ('Unlock\nUse fingerprint to unlock\nCharging 7%', 'locked'),
                                       ('Session expired\nPlease log in again.\nOK', 'session_expired')])
def test_location_blockers_return_immediately_instead_of_waiting_full_timeout(monkeypatch, text, state):
    monkeypatch.setattr(android, 'open_facebook_page', lambda serial, url, **kwargs: None)
    monkeypatch.setattr(android, 'screen_text', lambda serial: text)
    monkeypatch.setattr(android.time, 'sleep', lambda seconds: pytest.fail('known blocker must not keep polling'))
    found = android.account_location('S', timeout=30)
    assert found.state == state
    assert found.location == ''
    assert state in found.describe()


def test_the_location_tool_defaults_are_single_sourced() -> None:
    """Same rule as the other android tools: the schema and the handler must not be
    able to disagree about the timeout."""
    from jev_use import mcp_server

    tools = {t["name"]: t for t in mcp_server.TOOLS}
    assert "android_location" in tools
    assert (
        tools["android_location"]["inputSchema"]["properties"]["timeout"]["default"]
        == android.LOCATION_LOAD_TIMEOUT
    )
    assert "android_location" in mcp_server.HANDLERS


def test_blank_webview_gets_one_bounded_read_only_recovery(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    opens, reads, backs = [], [], []
    monkeypatch.setattr(android.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(android.time, "sleep", clock.sleep)
    monkeypatch.setattr(android, "open_facebook_page", lambda serial, url, **kwargs: opens.append(url))
    monkeypatch.setattr(android, "key", lambda serial, code: backs.append(code))
    monkeypatch.setattr(android, "screen_text", lambda serial: reads.append(1) or (
        "Your primary location is near:\nLahore, Punjab 54" if len(reads) >= 4 else ""))
    webview = android.Observation("S", android.parse_nodes(
        '<hierarchy><node class="android.webkit.WebView" package="com.facebook.katana" '
        'bounds="[0,0][1080,2160]" /></hierarchy>'), (1080, 2160), "com.facebook.katana/.WebView")
    monkeypatch.setattr(android, "snapshot", lambda serial: webview)

    found = android.account_location("S", timeout=8, poll=1)

    assert found.state == "location"
    assert found.location == "Lahore, Punjab 54"
    assert found.retries == 1
    assert len(opens) == 2
    assert backs == [android.KEYCODES["go_back"]]
    assert found.timings["recovery"] >= 0


def test_blank_non_webview_does_not_trigger_navigation_recovery(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    opens, backs = [], []
    monkeypatch.setattr(android.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(android.time, "sleep", clock.sleep)
    monkeypatch.setattr(android, "open_facebook_page", lambda serial, url, **kwargs: opens.append(url))
    monkeypatch.setattr(android, "key", lambda serial, code: backs.append(code))
    monkeypatch.setattr(android, "screen_text", lambda serial: "")
    monkeypatch.setattr(android, "snapshot", lambda serial: android.Observation(
        "S", [], (1080, 2160), "com.android.launcher/.Home"))

    found = android.account_location("S", timeout=3, poll=1)

    assert found.state == "unknown"
    assert found.retries == 0
    assert len(opens) == 1
    assert backs == []


def test_slow_recovery_inspection_does_not_act_after_deadline(monkeypatch):
    now = [0.0]
    opens = []
    monkeypatch.setattr(android.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(android, "open_facebook_page", lambda serial, url, **kwargs: opens.append(url))

    def blank_read(serial):
        now[0] += 2
        return ""

    def slow_inspection(serial):
        now[0] += 5
        return android.Observation("S", [], (1080, 2160), "com.facebook.katana/.WebView")

    monkeypatch.setattr(android, "screen_text", blank_read)
    monkeypatch.setattr(android, "snapshot", slow_inspection)
    monkeypatch.setattr(android, "key", lambda *args: pytest.fail("deadline expired during inspection"))
    found = android.account_location("S", timeout=5, poll=0)
    assert found.state == "unknown"
    assert found.retries == 0
    assert len(opens) == 1
