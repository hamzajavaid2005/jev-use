"""Tests for the browser-only engine and its MCP surface."""

from __future__ import annotations

import itertools
import json
import time

import pytest

from jev_use import browser, mcp_server
from jev_use.driver import DriverError
from jev_use.profiles import LocalProfile


# -- parsing ----------------------------------------------------------------


def test_snapshot_js_is_valid_javascript_shape() -> None:
    """The table script must call querySelectorAll and return JSON."""
    assert "querySelectorAll" in browser.TABLE_JS
    assert "data-jev-ref" in browser.TABLE_JS
    assert "JSON.stringify" in browser.TABLE_JS


def test_click_js_uses_an_integer_ref_never_page_text() -> None:
    js = browser.click_js(7)
    assert "'[data-jev-ref=\"7\"]'" in js
    assert "e.click()" in js


def test_navigate_js_clears_beforeunload_first() -> None:
    """A `beforeunload` prompt blocks the CDP call and hangs the tool; the handler is
    cleared just before we navigate."""
    js = browser.navigate_js("https://x.test/a")
    assert "onbeforeunload = null" in js
    assert "https://x.test/a" in js


def test_click_js_refuses_a_non_integer_ref() -> None:
    """Fail closed. A ref is always one of our own integers; anything else is a bug,
    and interpolating it into the script would be an injection."""
    with pytest.raises(ValueError):
        browser.click_js("1']; alert(1); //")  # type: ignore[arg-type]


def test_element_center_js_scrolls_then_reports_the_centre() -> None:
    """A coordinate click must be aimed at somewhere real: scroll it in, then measure
    it — and refuse an off-screen point rather than clicking whatever is there."""
    js = browser.element_center_js(7)
    assert "'[data-jev-ref=\"7\"]'" in js
    assert "scrollIntoView" in js
    assert "getBoundingClientRect" in js
    assert "innerWidth" in js, "the viewport bound is what makes the refusal possible"


def test_element_center_js_refuses_a_non_integer_ref() -> None:
    with pytest.raises(ValueError):
        browser.element_center_js("1']; alert(1); //")  # type: ignore[arg-type]


class ClickHarness:
    """A transport with an input channel, recording what it was asked to do."""

    def __init__(self, point: dict) -> None:
        self.point = point
        self.clicked: list[tuple[float, float, str | None]] = []
        self.scripts: list[str] = []

    def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
        self.scripts.append(javascript)
        if "e.click()" in javascript:
            return json.dumps({"ok": True, "tag": "button"})
        return json.dumps(self.point)

    def click_at(self, x: float, y: float, url_hint: str | None = None) -> None:
        self.clicked.append((x, y, url_hint))


TARGET = browser.Target(port=53142, pid=0, window_id=0, url_hint="https://x.test/p")


def test_a_transport_with_an_input_channel_is_clicked_at_the_element_centre() -> None:
    """A real mouse event is the point: `element.click()` is `isTrusted: false`, and
    anything that checks (file inputs, drag/drop, upload widgets) ignores it."""
    session = ClickHarness({"ok": True, "x": 12.5, "y": 40, "tag": "button"})

    browser.click(session, TARGET, 3)

    assert session.clicked == [(12.5, 40, "https://x.test/p")]
    assert not any("e.click()" in script for script in session.scripts), (
        "a trusted click must not also fire a synthetic one"
    )


def test_an_unclickable_centre_falls_back_to_the_dom_click() -> None:
    session = ClickHarness({"ok": False, "reason": "outside viewport"})

    browser.click(session, TARGET, 3)

    assert session.clicked == [], "never aim a coordinate click at a point that is not there"
    assert any("e.click()" in script for script in session.scripts)


def test_a_transport_without_an_input_channel_uses_the_dom_click() -> None:
    class NoInput:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
            self.scripts.append(javascript)
            return json.dumps({"ok": True, "tag": "button"})

    session = NoInput()
    browser.click(session, TARGET, 3)
    assert any("e.click()" in script for script in session.scripts)


def test_a_failed_click_is_reported() -> None:
    class NeverClicks:
        def evaluate(self, javascript: str, url_hint: str | None = None) -> str:
            return json.dumps({"ok": False, "reason": "ref not found"})

    with pytest.raises(DriverError):
        browser.click(NeverClicks(), TARGET, 3)


def test_parse_js_payload_handles_the_prose_envelope() -> None:
    envelope = '\u2705 Ran script:\n{"url":"https://x.test","title":"t","elements":[]}'
    payload = browser._parse_js_payload(envelope)
    assert payload["url"] == "https://x.test"


def test_parse_js_payload_handles_bare_json() -> None:
    payload = browser._parse_js_payload('{"url":"https://y.test","elements":[{"ref":0}]}')
    assert payload["url"] == "https://y.test"


def test_parse_js_payload_rejects_nonsense() -> None:
    with pytest.raises(DriverError):
        browser._parse_js_payload("no json here at all")


# -- discovery --------------------------------------------------------------


def test_running_profiles_parses_the_process_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table now comes from `host`, so the seam is platform-neutral: the
    Windows implementation feeds the same (pid, argv) rows as `ps` does."""
    table = [
        (
            43886,
            "/opt/google/chrome/chrome --remote-debugging-port=9222 "
            "--user-data-dir=/home/h/.config/google-chrome",
        ),
        (
            43895,
            "/opt/google/chrome/chrome --type=renderer "
            "--user-data-dir=/home/h/.config/google-chrome",
        ),
        (43939, "/opt/google/chrome/chrome --type=gpu-process --user-data-dir=/x"),
        (1, "/sbin/init"),
    ]
    monkeypatch.setattr(browser, "_process_table", lambda *a, **k: table)
    profiles = browser.running_profiles()

    assert len(profiles) == 1, "renderers, gpu and unrelated processes are filtered out"
    assert profiles[0].pid == 43886
    assert profiles[0].port == 9222
    assert profiles[0].cdp is True
    assert "google-chrome" in profiles[0].profile_dir


def test_running_profiles_matches_the_real_binary_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chrome's surviving process is /opt/google/chrome/chrome, not the wrapper.

    Matching only 'google-chrome' works while the launch argv is intact and then
    silently stops — discovery must use the real path too.
    """
    monkeypatch.setattr(
        browser,
        "_process_table",
        lambda *a, **k: [
            (
                11035,
                "/opt/google/chrome/chrome --remote-debugging-port=9333 "
                "--user-data-dir=/tmp/jev-cdp-test",
            )
        ],
    )
    profiles = browser.running_profiles()
    assert len(profiles) == 1
    assert profiles[0].port == 9333
    assert profiles[0].profile_dir == "/tmp/jev-cdp-test"


def test_running_profiles_reads_a_windows_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows shape: a quoted argv[0] with spaces, `chrome.exe`, and a quoted
    --user-data-dir value. A bare `\\S+` capture would keep the quotes and the
    profile path would never match a real directory."""
    command_line = (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port=9222 '
        '--user-data-dir="C:\\Users\\me\\AppData\\Local\\jev-use\\profiles\\default"'
    )
    monkeypatch.setattr(
        browser,
        "_process_table",
        lambda *a, **k: [
            (4242, command_line),
            (
                4243,
                '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                "--type=renderer",
            ),
        ],
    )
    profiles = browser.running_profiles()
    assert len(profiles) == 1, "the renderer child is filtered out"
    assert profiles[0].pid == 4242
    assert profiles[0].port == 9222
    assert profiles[0].profile_dir == "C:\\Users\\me\\AppData\\Local\\jev-use\\profiles\\default"
    assert '"' not in profiles[0].profile_dir, "the surrounding quotes are stripped"


def test_running_profiles_ignores_other_tools_named_chrome() -> None:
    """The measured bug: chrome-devtools-mcp is an MCP server, not a browser.

    Substring-matching "chrome" anywhere in the argv reported phantom profiles for
    it, which showed up as extra rows in browser_profiles and made the setup script
    refuse forever with Chrome closed.
    """
    assert browser._is_browser_process("npm exec chrome-devtools-mcp@latest") is False
    assert browser._is_browser_process("sh -c chrome-devtools-mcp") is False
    assert browser._is_browser_process(
        "node /home/h/.npm/_npx/abc/node_modules/chrome-devtools-mcp/build/index.js"
    ) is False
    assert browser._is_browser_process("pgrep -x chrome") is False


def test_running_profiles_accepts_real_browser_binaries() -> None:
    assert browser._is_browser_process("/opt/google/chrome/chrome --remote-debugging-port=9333")
    assert browser._is_browser_process("google-chrome --user-data-dir=/tmp/x")
    assert browser._is_browser_process("/usr/bin/chromium-browser")
    assert browser._is_browser_process("/snap/bin/brave-browser --foo")


def test_running_profiles_accepts_windows_binaries() -> None:
    """`chrome.exe` is the same browser as `chrome`; only the extension differs,
    and Windows quotes argv[0] whenever the path contains a space."""
    assert browser._is_browser_process(
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        "--remote-debugging-port=9222"
    )
    assert browser._is_browser_process(r"C:\Chrome\chrome.exe")
    assert browser._is_browser_process(
        r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe --user-data-dir=C:\x"
    )


def test_windows_devtools_helper_is_not_a_browser() -> None:
    """The phantom-profile bug, in its Windows spelling."""
    assert (
        browser._is_browser_process(
            '"C:\\Program Files\\nodejs\\node.exe" '
            "C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\chrome-devtools-mcp\\index.js"
        )
        is False
    )


def test_running_profiles_defaults_the_profile_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "_process_table", lambda *a, **k: [(99, "/usr/bin/google-chrome")])
    profiles = browser.running_profiles()
    assert profiles[0].profile_dir == str(browser.DEFAULT_PROFILE)
    assert profiles[0].port is None
    assert profiles[0].cdp is False


def test_cdp_alive_is_false_when_nothing_answers() -> None:
    assert browser.cdp_alive(9, timeout=0.2) is False


# -- GoLogin / antidetect browsers ------------------------------------------
#
# Orbita is Chromium under GoLogin's launcher: the executable is plain `chrome`
# on Linux and Windows, and the debug port is chosen at random. The
# `--gologin-profile` flag is what identifies it, and the profile name is on the
# command line rather than the (temporary) profile path.


def test_gologin_flag_identifies_a_browser_the_name_cannot() -> None:
    assert browser._is_browser_process("/opt/weird/vendor-bin --gologin-profile=Work")


def test_gologin_orbita_is_known_on_macos_by_name() -> None:
    # The macOS build is `Orbita`, not `chrome`, and carries no extra flag here.
    assert browser._is_browser_process(
        "/Users/me/.gologin/browser/orbita-browser-132/Orbita-Browser.app/Contents/MacOS/Orbita"
    )


def test_running_profiles_labels_a_gologin_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser,
        "_process_table",
        lambda *a, **k: [
            (
                555,
                "/tmp/gologin_abc/chrome --remote-debugging-port=53142 "
                "--user-data-dir=/tmp/gologin_abc --gologin-profile=Acme Ads",
            ),
        ],
    )
    profiles = browser.running_profiles()

    assert len(profiles) == 1
    assert profiles[0].vendor == "gologin"
    assert profiles[0].name == "Acme Ads"
    assert profiles[0].port == 53142
    assert profiles[0].label == "Acme Ads", "the human name comes from the flag, not the tmp dir"
    assert "[gologin]" in profiles[0].describe()


def test_a_gologin_browser_is_not_mistaken_for_the_phantom_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker must not fire on an unrelated command line that merely mentions
    GoLogin (a CLI, a log path) — only on the launch flag."""
    assert browser._is_browser_process("npm exec gologin-cli -- list-profiles") is False
    assert browser._is_browser_process("tail -f /var/log/gologin.log") is False


def test_a_url_is_not_evidence_of_the_vendor() -> None:
    """A normal Chrome opened on a gologin.com page must not be labelled GoLogin —
    only the executable and the profile directory count."""
    args = (
        "/opt/google/chrome/chrome --user-data-dir=/home/me/.config/google-chrome "
        "https://app.gologin.com/profile"
    )
    assert browser._vendor(args) == "chrome"

    orbita = "/home/me/.gologin/browser/orbita-browser-132/chrome --user-data-dir=/tmp/gologin_x"
    assert browser._vendor(orbita) == "gologin"


# -- attach -----------------------------------------------------------------


def test_attach_explains_the_fix_when_no_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "running_profiles", lambda: [])

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must not touch the driver when there is no port")

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver())
    message = str(excinfo.value)
    assert "enable-cdp.sh" in message, "the error must name the remediation"
    assert "/json/version" in message or "CDP" in message


def test_attach_rejects_a_live_but_unresponsive_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 1111)

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must not proceed")

    with pytest.raises(DriverError):
        browser.attach(FakeDriver(), port=2222)


def test_attach_refuses_rather_than_driving_the_wrong_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured bug: a stray Chrome with a CDP port must NOT be used when the
    caller asked for their real profile, which has none."""
    real = browser.Profile(
        pid=100, profile_dir=str(browser.DEFAULT_PROFILE), port=None
    )
    stray = browser.Profile(pid=200, profile_dir="/tmp/some-other-profile", port=9999)
    monkeypatch.setattr(browser, "running_profiles", lambda: [real, stray])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 9999)

    class FakeDriver:
        def call(self, *a, **k):
            raise AssertionError("must refuse before touching the driver")

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver())
    assert "without a CDP endpoint" in str(excinfo.value)
    assert "enable-cdp.sh" in str(excinfo.value)


def test_attach_honours_an_explicit_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    wanted = browser.Profile(pid=300, profile_dir="/home/h/.config/google-chrome-work", port=9223)
    default = browser.Profile(pid=100, profile_dir=str(browser.DEFAULT_PROFILE), port=None)
    monkeypatch.setattr(browser, "running_profiles", lambda: [default, wanted])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: True)

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    return {"windows": [{"pid": 300, "window_id": 7, "app_name": "Google Chrome", "title": "x"}]}

            return R()

    target = browser.attach(FakeDriver(), profile="work")
    assert target.port == 9223
    assert target.pid == 300


def test_attach_binds_an_explicit_port_that_discovery_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GoLogin's port is random and its process is not always in the table, so the
    explicit port is the route that reaches it — no profile matching involved."""
    monkeypatch.setattr(browser, "running_profiles", lambda: [])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: port == 53142)

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    return {"windows": [{"pid": 2, "window_id": 5, "app_name": "Orbita", "title": "x"}]}

            return R()

    target = browser.attach(FakeDriver(), port=53142)
    assert target.port == 53142
    assert target.pid == 2


def test_attach_matches_a_gologin_profile_by_its_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gologin = browser.Profile(
        pid=9, profile_dir="/tmp/gologin_abc", port=53142, vendor="gologin", name="Acme Ads"
    )
    monkeypatch.setattr(browser, "running_profiles", lambda: [gologin])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: True)

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    # Orbita names its window `Orbita`; the bind must still find it.
                    return {
                        "windows": [
                            {"pid": 9, "window_id": 3, "app_name": "Orbita", "title": "Acme Ads"}
                        ]
                    }

            return R()

    target = browser.attach(FakeDriver(), profile="acme")
    assert target.port == 53142
    assert target.pid == 9


def test_attach_refuses_a_dead_explicit_port_with_a_useful_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser, "running_profiles", lambda: [])
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: False)

    with pytest.raises(DriverError) as excinfo:
        browser.attach(object(), port=53142)  # type: ignore[arg-type]
    assert "53142" in str(excinfo.value)
    assert "GoLogin" in str(excinfo.value)


def test_attach_names_available_profiles_on_a_bad_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser, "running_profiles",
        lambda: [browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=9222)],
    )
    with pytest.raises(DriverError) as excinfo:
        browser.attach(object(), profile="firefox-profile")  # type: ignore[arg-type]
    assert "google-chrome" in str(excinfo.value)


def test_attach_requires_a_chrome_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "cdp_alive", lambda port, timeout=1.5: True)
    monkeypatch.setattr(
        browser, "running_profiles",
        lambda: [browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=9222)],
    )

    class FakeDriver:
        def call(self, tool, args=None):
            class R:
                def json(self):
                    return {"windows": [{"pid": 1, "window_id": 2, "app_name": "Files", "title": "Home"}]}

            return R()

    with pytest.raises(DriverError) as excinfo:
        browser.attach(FakeDriver(), port=9222)
    assert "no Chrome window" in str(excinfo.value)


# -- observation ------------------------------------------------------------


PAYLOAD = {
    "url": "https://example.test/page",
    "title": "Example",
    "elements": [
        {"ref": 0, "tag": "a", "text": "Learn more"},
        {"ref": 1, "tag": "input", "text": "Search"},
        {"ref": 2, "tag": "button", "text": ""},
    ],
}


def make_observation(navigate_url: str | None = None) -> browser.Observation:
    target = browser.Target(port=9222, pid=1, window_id=2)
    return browser.Observation(target, PAYLOAD, navigate_url)


def test_candidates_are_page_scoped_and_described() -> None:
    observation = make_observation()
    assert len(observation.candidates) == 3
    assert observation.candidates[0]["description"] == 'a "Learn more"'
    assert observation.candidates[2]["description"] == "button", "unlabelled falls back to tag"


def test_target_map_is_keyed_by_ref() -> None:
    assert set(make_observation().target_map()) == {"0", "1", "2"}


def test_duplicate_labels_are_disambiguated_not_dropped() -> None:
    """Two controls that share a label must both stay selectable, or a real target
    is lost; the hint is what keeps the option set mutually exclusive."""
    payload = {
        "url": "u",
        "elements": [
            {"ref": 0, "tag": "button", "text": "Add to cart", "pos": "top-right"},
            {"ref": 1, "tag": "button", "text": "Add to cart", "pos": "bottom-right"},
        ],
    }
    observation = browser.Observation(browser.Target(port=1, pid=2, window_id=3), payload)
    descriptions = [c["description"] for c in observation.candidates]
    assert len(descriptions) == 2, "both targets survive"
    assert len(set(descriptions)) == 2, "and neither reads as a duplicate of the other"
    assert "top-right" in descriptions[0]
    assert "bottom-right" in descriptions[1]


def test_a_huge_page_is_capped_goal_first() -> None:
    elements = [
        {"ref": i, "tag": "a", "text": f"link {i}"} for i in range(200)
    ]
    elements.append({"ref": 999, "tag": "input", "text": "Checkout", "fillable": True})
    elements.append({"ref": 1000, "tag": "button", "text": "Checkout now"})
    observation = browser.Observation(
        browser.Target(port=1, pid=2, window_id=3),
        {"url": "u", "elements": elements},
        goal="finish checkout",
    )
    assert len(observation.candidates) == browser.MAX_CANDIDATES
    labels = {c["label"] for c in observation.candidates}
    assert "Checkout" in labels, "the field the goal needs must survive the cap"
    assert "Checkout now" in labels, "and so must the goal-relevant button"


def test_the_loop_decides_an_unambiguous_first_step_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clear goal must not cost a model round trip on the first step."""

    class Refuses:
        def choose(self, *_a, **_k):
            pytest.fail("the chooser must not be consulted for a clear goal")

    observation = make_observation()
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: observation)
    result = browser._run_one(
        None, target(), "click Learn more", Refuses(),
        act=False, max_steps=1, min_confidence=0.4, settle=0.0,
    )
    assert result.steps[0].decision.source == "deterministic"
    assert result.steps[0].decision.element_id == "0"


def test_the_loop_settles_after_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typing can trigger validation, autocomplete or a same-URL update; the next
    snapshot must not race it."""
    from jev_use.choosers import Decision

    target_obj = browser.Target(port=1, pid=2, window_id=3)
    obs = browser.Observation(
        target_obj,
        {"url": "u", "elements": [{"ref": 0, "tag": "input", "text": "Email", "fillable": True}]},
        can_write=True,
    )
    settled: list[str] = []
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: obs)
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: BEFORE)
    monkeypatch.setattr(browser, "type_into", lambda *a, **k: None)
    monkeypatch.setattr(
        browser, "_wait_for_settle", lambda d, t, b, s: settled.append("yes")
    )

    class Chooser:
        def choose(self, *_a):
            return Decision(kind="type_text", element_id="0", confidence=0.9)

    class Writer:
        available = True

        def write(self, *_a, **_k):
            return "me@example.com"

    browser._run_one(
        None, target_obj, "fill Email", Chooser(),
        act=True, max_steps=1, min_confidence=0.4, settle=0.5, writer=Writer(),
    )
    assert settled == ["yes"], "typing must settle before the next snapshot"


def test_replay_refuses_an_ambiguous_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two elements matching one stored description must not silently pick the first."""
    target_obj = browser.Target(port=1, pid=2, window_id=3)
    obs = browser.Observation(
        target_obj, {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "Open"}]}
    )
    obs._candidates.append(dict(obs._candidates[0], id="9"))
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: obs)
    monkeypatch.setattr(browser, "execute", lambda *a, **k: None)

    result = browser.replay(
        None, target_obj, "g", [{"kind": "click_element", "target": 'a "Open"'}], act=True
    )
    assert result.outcome == "replay_miss"
    assert "2 elements match" in result.steps[0].note


def test_the_table_script_filters_duplicates_and_decoration() -> None:
    """The DOM-aware half of candidate filtering lives in the injected script."""
    assert "removeAttribute('data-jev-ref')" in browser.TABLE_JS, (
        "stale markers from the previous scan must be cleared, or click_js can"
        " resolve a ref to an element filtered out this time"
    )
    assert "accepted.has" in browser.TABLE_JS, "nested duplicate controls, via a Set"
    assert "javascript:" in browser.TABLE_JS, "label-less decorative links"
    assert "pos:" in browser.TABLE_JS, "a position hint for disambiguation"


def test_operations_hide_click_when_the_page_offers_nothing() -> None:
    target = browser.Target(port=9222, pid=1, window_id=2)
    empty = browser.Observation(target, {"url": "u", "elements": []})
    assert "click_element" not in empty.operations()

    assert "click_element" in make_observation().operations()


def test_navigate_is_only_offered_when_the_goal_names_a_url() -> None:
    assert "navigate" not in make_observation().operations()
    assert "navigate" in make_observation("https://target.test").operations()


FIELD_PAYLOAD = {
    "url": "https://example.test/login",
    "title": "Sign in",
    "elements": [
        {"ref": 0, "tag": "a", "text": "Forgot password"},
        {"ref": 1, "tag": "input", "text": "Email", "fillable": True},
        {"ref": 2, "tag": "input", "text": "Password", "fillable": True},
        {"ref": 3, "tag": "button", "text": "Sign in"},
    ],
}


def make_field_observation(can_write: bool = True) -> browser.Observation:
    target = browser.Target(port=9222, pid=1, window_id=2)
    return browser.Observation(target, FIELD_PAYLOAD, can_write=can_write)


def test_type_text_is_withheld_without_a_writer() -> None:
    """Jev cannot generate the string, so offering the operation would be a trap."""
    assert "type_text" not in make_field_observation(can_write=False).operations()


def test_type_text_is_offered_when_a_writer_exists() -> None:
    assert "type_text" in make_field_observation(can_write=True).operations()


def test_type_text_is_withheld_when_there_is_nowhere_to_type() -> None:
    target = browser.Target(port=9222, pid=1, window_id=2)
    no_fields = browser.Observation(
        target,
        {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "link"}]},
        can_write=True,
    )
    assert "type_text" not in no_fields.operations()


def test_text_head_contains_only_fillable_elements() -> None:
    heads = make_field_observation().target_heads()
    assert set(heads["text_target"]) == {"1", "2"}, "only the two inputs"
    assert set(heads["click_target"]) == {"0", "1", "2", "3"}, "all are clickable"


def test_targets_for_returns_the_legal_set_per_operation() -> None:
    observation = make_field_observation()
    assert {c["id"] for c in observation.targets_for("type_text")} == {"1", "2"}
    assert len(observation.targets_for("click_element")) == 4


def test_type_js_encodes_the_value_rather_than_interpolating() -> None:
    js = browser.type_js(3, 'he said "hi"; alert(1)')
    assert "alert(1)" in js, "the text is present"
    assert '\\"hi\\"' in js or '\\"' in js, "but escaped, as a JSON string literal"
    assert "data-jev-ref=\"3\"" in js


def test_type_js_dispatches_the_events_frameworks_listen_for() -> None:
    """Setting .value alone is invisible to React."""
    js = browser.type_js(0, "x")
    assert "getOwnPropertyDescriptor" in js
    assert "new Event('input'" in js
    assert "new Event('change'" in js


# -- readiness --------------------------------------------------------------


class FakeJsDriver:
    """Feeds scripted snapshots so the wait logic can be tested without a browser."""

    def __init__(self, samples: list[dict]) -> None:
        self.samples = samples
        self.calls = 0

    def call(self, tool, args=None):
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1

        class R:
            text = "cdp.runtime.evaluate.user_gesture: " + json.dumps(json.dumps(sample))

        return R()


def target() -> browser.Target:
    return browser.Target(port=1, pid=2, window_id=3)


def test_read_always_settles_even_on_a_large_page() -> None:
    """The measured Cloudflare bug: 1603 characters of nav chrome cleared a
    "substantial" shortcut before any billing figure had rendered. Length cannot
    tell a shell from a page."""
    shell = "Billing R2 Object Storage Storage and databases " * 40  # > 1000 chars
    full = shell + " " + ("Total due $1.41 " * 20)
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 300, "text": shell},
            {"url": "u", "state": "complete", "nodes": 900, "text": full},
            {"url": "u", "state": "complete", "nodes": 900, "text": full},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "Total due" in text, "must not return the shell just because it is long"


def test_read_keeps_waiting_while_the_page_is_still_blank() -> None:
    """The measured regression: settling on empty text returned 0 characters from
    Cloudflare instead of waiting for it to fill in."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 40, "text": ""},
            {"url": "u", "state": "complete", "nodes": 40, "text": ""},
            {"url": "u", "state": "complete", "nodes": 800, "text": "billing total $1.41"},
        ]
    )
    assert "billing total" in browser.read(driver, target(), timeout=5)


def test_read_is_not_fooled_by_a_ticking_clock() -> None:
    """Text that never stops changing must not block the read: settle on the DOM."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 500, "text": "page 12:00:01"},
            {"url": "u", "state": "complete", "nodes": 500, "text": "page 12:00:02"},
        ]
    )
    assert browser.read(driver, target(), timeout=5) == "page 12:00:02"


def test_read_waits_for_a_shell_to_fill_in() -> None:
    """The measured Vercel bug: a 271-char nav sidebar returned while the data table
    was still loading, and the agent blamed a stuck filter."""
    shell = "Find Projects Deployments Logs Analytics Overview Settings" * 4
    full = shell + " " + ("deployment row ready " * 100)
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "text": shell},
            {"url": "u", "state": "complete", "text": full},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "deployment row ready" in text


def test_read_waits_past_a_blank_spa_shell() -> None:
    """The measured bug: Cloudflare and Vercel both read back empty."""
    driver = FakeJsDriver(
        [
            {"url": "u", "state": "complete", "nodes": 10, "text": ""},
            {"url": "u", "state": "complete", "nodes": 200, "text": "loading"},
            {"url": "u", "state": "complete", "nodes": 900, "text": "the actual page content " * 4},
        ]
    )
    text = browser.read(driver, target(), timeout=5)
    assert "the actual page content" in text
    assert driver.calls >= 3


def test_read_returns_a_settled_short_page_rather_than_timing_out() -> None:
    """A genuinely sparse page must not cost the full timeout."""
    driver = FakeJsDriver([{"url": "u", "state": "complete", "nodes": 9, "text": "hi"}])
    assert browser.read(driver, target(), timeout=5) == "hi"
    assert driver.calls == 2, "returns once the text has settled"


def test_read_can_skip_waiting_entirely() -> None:
    driver = FakeJsDriver([{"url": "u", "state": "loading", "text": ""}])
    assert browser.read(driver, target(), wait=False) == ""
    assert driver.calls == 1


# -- settling ---------------------------------------------------------------


BEFORE = browser.PageSignature("u", "complete", 10, 100, 0)


def _sig(
    *, url: str = "u", state: str = "complete", nodes: int = 10, text: int = 100, fields: int = 0
) -> browser.PageSignature:
    return browser.PageSignature(url, state, nodes, text, fields)


def test_wait_for_settle_waits_when_the_page_has_not_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression this function was fixed for: a click that starts a slow
    request leaves the page perfectly still. Stillness is not settled, so the wait
    must run out `settle` instead of returning the pre-update page."""
    monkeypatch.setattr(
        browser, "snapshot", lambda *a, **k: pytest.fail("the poll must not snapshot")
    )
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: BEFORE)
    started = time.perf_counter()
    assert browser._wait_for_settle(object(), target(), BEFORE, 0.5) is None
    assert time.perf_counter() - started >= 0.4, "no early exit without a change"


def test_wait_for_settle_returns_once_a_change_goes_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed from the pre-action state, then stable, returns inside the window."""
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: _sig(nodes=20, text=200))
    monkeypatch.setattr(
        browser, "snapshot", lambda *a, **k: pytest.fail("the poll must not snapshot")
    )
    started = time.perf_counter()
    assert browser._wait_for_settle(None, target(), BEFORE, 2.0) is None
    assert time.perf_counter() - started < 1.5, "changed then quiet returns early"


def test_wait_for_settle_sees_a_text_only_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Element count alone misses a text-only update, so text length is in the
    signature too."""
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: _sig(text=200))
    started = time.perf_counter()
    assert browser._wait_for_settle(None, target(), BEFORE, 2.0) is None
    assert time.perf_counter() - started < 1.5


def test_wait_for_settle_sees_a_value_only_change() -> None:
    """Typing changes `input.value`, which never appears in `body.innerText`, so the
    signature must carry form state or a normal field entry looks like "no change"."""
    assert browser._dom_changed(BEFORE, _sig(fields=12))
    assert "input,textarea,select" in browser.SETTLE_JS


def test_wait_for_settle_does_not_return_while_still_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL change with `readyState == 'loading'` is a half-loaded document, not a
    settled one, so it must not end the wait."""
    monkeypatch.setattr(
        browser, "_page_signature",
        lambda *a, **k: _sig(url="https://new", state="loading", nodes=5, text=5),
    )
    started = time.perf_counter()
    assert browser._wait_for_settle(None, target(), BEFORE, 0.5) is None
    assert time.perf_counter() - started >= 0.4, "must not return mid-navigation"


def test_wait_for_settle_does_not_return_while_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`interactive` is not `complete`: scripts and data are still arriving."""
    monkeypatch.setattr(
        browser, "_page_signature",
        lambda *a, **k: _sig(url="https://new", state="interactive", nodes=5, text=5),
    )
    started = time.perf_counter()
    assert browser._wait_for_settle(None, target(), BEFORE, 0.5) is None
    assert time.perf_counter() - started >= 0.4


def test_wait_for_settle_returns_the_new_url_once_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser, "_page_signature",
        lambda *a, **k: _sig(url="https://new", state="complete", nodes=5, text=5),
    )
    assert browser._wait_for_settle(None, target(), BEFORE, 2.0) == "https://new"


def test_wait_for_settle_honours_the_deadline_when_the_page_keeps_changing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that never goes quiet must not block: `settle` stays the hard bound."""
    state = {"n": 0}

    def sig(*_a, **_k):
        state["n"] += 1
        return _sig(nodes=10 + state["n"], text=100 + state["n"])

    monkeypatch.setattr(browser, "_page_signature", sig)
    started = time.perf_counter()
    assert browser._wait_for_settle(None, target(), BEFORE, 0.5) is None
    assert time.perf_counter() - started < 2.0


def test_navigate_and_settle_baselines_on_the_live_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting against the target's cached url could be stale or empty, so the
    baseline is read from the page immediately before navigating."""
    order: list[str] = []
    monkeypatch.setattr(
        browser,
        "_page_signature",
        lambda *a: (order.append("signature"), BEFORE)[1],
    )
    monkeypatch.setattr(browser, "_js", lambda *a: order.append("js") or "{}")
    monkeypatch.setattr(browser, "_wait_for_settle", lambda *a: None)
    browser.navigate_and_settle(None, target(), "https://dest", 0.3)
    assert order == ["signature", "js"], "read the live URL before navigating"


# -- reading many pages -----------------------------------------------------


class _FakeReadDriver:
    """A driver whose `page` call always answers with one fixed page."""

    def __init__(self, text: str = "Hello page") -> None:
        self.text = text
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def call(self, tool, args=None):
        outer = "cdp.runtime.evaluate.user_gesture: " + json.dumps(
            json.dumps({"url": "u", "state": "complete", "nodes": 5, "text": self.text})
        )

        class R:
            text = outer

        return R()


def test_read_many_reads_every_url_and_closes_its_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = ["https://a.test", "https://b.test", "https://c.test"]
    opened: list[str] = []
    closed: list[str] = []
    ids = itertools.count(1)
    monkeypatch.setattr(
        browser, "open_tab",
        lambda port, url: opened.append(url) or {"id": f"t{next(ids)}", "url": url},
    )
    monkeypatch.setattr(browser, "close_tab", lambda port, tab_id: closed.append(tab_id))

    results = browser.read_many(
        browser.Target(port=9222, pid=1, window_id=2), urls,
        concurrency=3, wait=False, new_driver=lambda: _FakeReadDriver("Page text"),
    )

    assert [u for u, _ in results] == urls, "results are returned in the order asked"
    assert all("Page text" in t for _, t in results)
    assert sorted(opened) == sorted(urls), "every url got a tab (opened concurrently)"
    assert len(closed) == 3, "every tab we opened is closed again"


def test_read_many_reports_one_bad_page_without_sinking_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def open_tab(port, url):
        return None if url.endswith("bad") else {"id": "t1", "url": url}

    monkeypatch.setattr(browser, "open_tab", open_tab)
    monkeypatch.setattr(browser, "close_tab", lambda *a: None)

    results = browser.read_many(
        browser.Target(port=9222, pid=1, window_id=2),
        ["https://ok.test", "https://bad"],
        concurrency=2, wait=False, new_driver=lambda: _FakeReadDriver("fine"),
    )
    assert results[0][1] == "fine"
    assert results[1][1].startswith("[error]")


def test_open_tab_uses_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome refuses the GET form of /json/new with 405."""
    seen: dict[str, str] = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.method
        seen["url"] = request.full_url

        class R:
            def read(self):
                return b'{"id":"t","url":"https://x.test"}'

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return R()

    monkeypatch.setattr(browser.urllib.request, "urlopen", fake_urlopen)
    tab = browser.open_tab(9222, "https://x.test/a b")
    assert seen["method"] == "PUT"
    assert tab["id"] == "t"
    assert "a%20b" in seen["url"], "the URL is quoted"


def test_read_many_concurrency_default_is_single_sourced() -> None:
    schema = {t["name"]: t for t in mcp_server.TOOLS}["browser_read_many"]["inputSchema"]
    assert schema["properties"]["concurrency"]["default"] == mcp_server.DEFAULT_READ_CONCURRENCY


def test_readouts_lead_with_url_and_title() -> None:
    readouts = make_observation().readouts()
    assert readouts[0] == "https://example.test/page"
    assert readouts[1] == "Example"


# -- the MCP surface --------------------------------------------------------


def test_surface_is_browser_and_android_only() -> None:
    """The desktop tools were removed deliberately; they are not coming back here.

    Android is not the desktop surface returning under another name: it goes
    through adb, so it needs no window binding and no accessibility tree.
    """
    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == [
        "browser_profiles",
        "browser_open",
        "browser_close",
        "browser_use",
        "browser_action",
        "browser_script",
        "browser_extract",
        "browser_read",
        "browser_read_many",
        "android_devices",
        "android_use",
        "android_read",
        "android_facebook",
        "android_facebook_flow",
        "android_location",
    ]
    assert not any("computer_use" in n for n in names)
    assert "list_windows" not in names
    assert "get_window_state" not in names


def test_every_tool_has_a_description_and_schema() -> None:
    for tool in mcp_server.TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_browser_script_uses_betterwright_without_starting_the_jev_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}
    released = []

    def fake_run_script(port, code, *, timeout, dismiss_overlays=True):
        seen.update(port=port, code=code, timeout=timeout)
        return {"ok": True, "result": "created"}

    monkeypatch.setattr(mcp_server.betterwright, "run_script", fake_run_script)
    monkeypatch.setattr(mcp_server.betterwright, "executable", lambda: "betterwright")
    monkeypatch.setattr(mcp_server, "reset_browser_session", lambda: released.append(True))

    result = mcp_server.tool_browser_script(
        {
            "port": 12345,
            "code": "return page.title()",
            "timeout": 30,
            "dismiss_overlays": False,
        }
    )

    assert seen == {"port": 12345, "code": "return page.title()", "timeout": 30.0}
    assert released == [True]
    assert '"result": "created"' in result


def test_browser_profiles_is_advertised_as_the_first_call() -> None:
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    assert "FIRST" in tools["browser_profiles"]["description"]


def test_browser_open_is_declared_with_the_copy_caveat() -> None:
    """The description must warn that a copy is a snapshot, not a live mirror."""
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    description = tools["browser_open"]["description"]
    assert "SNAPSHOT" in description
    assert "refresh" in description, "must name the escape hatch"
    assert "default data directory" in description, "must say WHY it copies"


def test_browser_open_reports_an_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_wanted):
        raise ValueError("no profile matches 'nope'. Available: Work, Home")

    monkeypatch.setattr(mcp_server, "find_profile", boom)
    text = mcp_server.tool_browser_open({"profile": "nope"})
    assert "Available: Work, Home" in text


def test_browser_open_short_circuits_when_already_drivable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    already = LocalProfile(directory="Profile 1", name="Work", prepared=True, port=9222)
    monkeypatch.setattr(mcp_server, "find_profile", lambda _w: already)
    monkeypatch.setattr(mcp_server, "cdp_alive", lambda port, timeout=1.5: True)
    text = mcp_server.tool_browser_open({"profile": "Work"})
    assert "already open" in text


# -- caching ----------------------------------------------------------------


def test_plan_stores_descriptions_never_refs() -> None:
    """Refs are snapshot-scoped integers; a description survives a page reload."""
    payload = {
        "url": "https://x.test",
        "elements": [{"ref": 7, "tag": "a", "text": "Learn more"}],
    }
    target = browser.Target(port=1, pid=2, window_id=3)
    observation = browser.Observation(target, payload)
    from jev_use.choosers import Decision

    step = browser.Step(0, Decision(kind="click_element", element_id="7"), observation, True)
    plan = browser.plan_from(browser.Result(goal="g", steps=[step]))
    assert plan == [{"kind": "click_element", "target": 'a "Learn more"'}]
    assert "7" not in str(plan)


def test_plan_skips_unexecuted_steps() -> None:
    payload = {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "x"}]}
    target = browser.Target(port=1, pid=2, window_id=3)
    observation = browser.Observation(target, payload)
    from jev_use.choosers import Decision

    step = browser.Step(0, Decision(kind="click_element", element_id="0"), observation, False)
    assert browser.plan_from(browser.Result(goal="g", steps=[step])) == []


def test_the_cache_key_fingerprints_controls_not_page_text() -> None:
    """A clock or a count must not change the key; a different control set must."""
    target = browser.Target(port=1, pid=2, window_id=3)
    base = {
        "url": "u",
        "title": "T",
        "elements": [{"ref": 0, "tag": "a", "text": "Learn more"}],
    }
    other_controls = dict(base, elements=[{"ref": 0, "tag": "a", "text": "Buy now"}])
    other_page = dict(base, url="v")

    assert browser.Observation(target, base).fingerprint() == (
        browser.Observation(target, base).fingerprint()
    )
    assert browser.Observation(target, base).fingerprint() != (
        browser.Observation(target, other_controls).fingerprint()
    )
    assert browser.Observation(target, base).fingerprint() != (
        browser.Observation(target, other_page).fingerprint()
    )


def test_a_cache_probe_is_reused_as_the_first_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On a cache miss the probe that built the key was discarded and the loop
    snapshotted again immediately. It seeds the first step now."""
    from jev_use.cache import PlanCache
    from jev_use.choosers import MockChooser

    calls: list[int] = []

    def fake_snapshot(driver, tg, goal="", can_write=False):
        calls.append(1)
        return browser.Observation(tg, {"url": "https://x.test", "elements": []})

    monkeypatch.setattr(browser, "snapshot", fake_snapshot)
    result = browser.run(
        object(), browser.Target(port=1, pid=2, window_id=3), "goal",
        MockChooser(script=[{"kind": "done"}]), act=True,
        cache=PlanCache(tmp_path / "c.json"),
    )
    assert result.outcome == "done"
    assert len(calls) == 1, "the probe must seed the first step, not be thrown away"


def test_a_partial_replay_miss_does_not_replan_from_a_stale_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The replay may act before it misses, so the probe no longer describes the
    screen; replanning must re-observe rather than reuse it."""
    from jev_use.cache import PlanCache
    from jev_use.choosers import Decision

    target_obj = browser.Target(port=1, pid=2, window_id=3)
    obs = browser.Observation(
        target_obj, {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "A"}]}
    )
    calls: list[str] = []

    def fake_snapshot(driver, tg, goal="", can_write=False):
        calls.append(goal)
        return obs

    monkeypatch.setattr(browser, "snapshot", fake_snapshot)
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: BEFORE)
    monkeypatch.setattr(browser, "_wait_for_settle", lambda *a, **k: None)
    monkeypatch.setattr(browser, "execute", lambda *a, **k: None)

    class Chooser:
        def choose(self, *_a):
            return Decision(kind="done")

    cache = PlanCache(tmp_path / "c.json")
    cache.put(
        f"{obs.fingerprint()}|g", "g",
        [{"kind": "click_element", "target": 'a "A"'},
         {"kind": "click_element", "target": 'a "Gone"'}],
    )

    browser.run(object(), target_obj, "g", Chooser(), act=True, cache=cache)
    # probe. Then the replay acts, re-observes, and misses. Then the re-plan must
    # take a THIRD, fresh snapshot rather than reuse the stale probe.
    assert len(calls) == 3, "the re-plan must re-observe"


def test_the_decomposition_path_does_not_reuse_the_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`read()` waits for the page to render, so the probe taken before it may
    predate the controls the plan will act on. The decomposition path re-observes."""
    from jev_use.cache import PlanCache
    from jev_use.choosers import Decision

    calls: list[str] = []

    def fake_snapshot(driver, tg, goal="", can_write=False):
        calls.append(goal)
        return browser.Observation(tg, {"url": "https://x.test", "elements": []})

    monkeypatch.setattr(browser, "snapshot", fake_snapshot)
    monkeypatch.setattr(browser, "read", lambda *a, **k: "")

    class Writer:
        available = True

        def decompose(self, goal, summary):
            return ["step one", "step two"]

        def write(self, *a, **k):
            return "x"

    class Chooser:
        def choose(self, *_a):
            return Decision(kind="done")

    browser.run(
        object(), browser.Target(port=1, pid=2, window_id=3), "g", Chooser(),
        act=True, writer=Writer(), cache=PlanCache(tmp_path / "c.json"),
    )
    assert calls.count("g") == 1, "the probe is taken once"
    assert calls == ["g", "step one", "step two"], "each subgoal is observed fresh"


def test_replay_reobserves_after_an_in_place_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-URL SPA change must not leave step two resolving against stale refs."""
    target_obj = browser.Target(port=1, pid=2, window_id=3)
    first = browser.Observation(
        target_obj, {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "Open"}]}
    )
    second = browser.Observation(
        target_obj, {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "Confirm"}]}
    )
    pages = iter([first, second])
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: next(pages))
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: BEFORE)
    monkeypatch.setattr(browser, "_wait_for_settle", lambda *a, **k: None)
    monkeypatch.setattr(browser, "execute", lambda *a, **k: None)

    result = browser.replay(
        None, target_obj, "g",
        [{"kind": "click_element", "target": 'a "Open"'},
         {"kind": "click_element", "target": 'a "Confirm"'}],
        act=True,
    )
    assert result.outcome == "replayed"
    assert result.snapshots == 2, "one initial + one re-observe between the entries"


def test_replay_aborts_when_a_cached_click_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale plan whose click no longer lands must not report success."""
    target_obj = browser.Target(port=1, pid=2, window_id=3)
    obs = browser.Observation(
        target_obj, {"url": "u", "elements": [{"ref": 0, "tag": "a", "text": "Go"}]}
    )
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: obs)
    monkeypatch.setattr(browser, "_page_signature", lambda *a, **k: BEFORE)
    monkeypatch.setattr(
        browser, "execute",
        lambda *a, **k: (_ for _ in ()).throw(DriverError("click failed: ref not found")),
    )
    result = browser.replay(
        None, target_obj, "g", [{"kind": "click_element", "target": 'a "Go"'}], act=True
    )
    assert result.outcome == "replay_miss"
    assert "click failed" in result.steps[0].note


def test_execute_fails_when_the_ref_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`click_js` answers `ok:false`; that must raise, not be ignored."""
    from jev_use.choosers import Decision

    monkeypatch.setattr(
        browser, "_js", lambda *a: '{"ok": false, "reason": "ref not found"}'
    )
    obs = browser.Observation(browser.Target(port=1, pid=2, window_id=3), {"url": "u", "elements": []})
    with pytest.raises(DriverError):
        browser.execute(
            None, browser.Target(port=1, pid=2, window_id=3),
            Decision(kind="click_element", element_id="0"), obs,
        )


def test_execute_propagates_a_driver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CDP/driver failure is not a completed click and must not be swallowed."""
    from jev_use.choosers import Decision

    def boom(*_a, **_k):
        raise DriverError("page failed: cdp connection closed")

    monkeypatch.setattr(browser, "_js", boom)
    obs = browser.Observation(
        browser.Target(port=1, pid=2, window_id=3), {"url": "u", "elements": []}
    )
    with pytest.raises(DriverError):
        browser.execute(
            None, browser.Target(port=1, pid=2, window_id=3),
            Decision(kind="click_element", element_id="0"), obs,
        )


def test_disabled_controls_are_not_candidates() -> None:
    """A disabled control cannot be activated, so offering it is an impossible
    choice that only costs a step."""
    payload = {
        "url": "u",
        "elements": [
            {"ref": 0, "tag": "button", "text": "Save", "enabled": True},
            {"ref": 1, "tag": "button", "text": "Delete", "enabled": False},
        ],
    }
    observation = browser.Observation(browser.Target(port=1, pid=2, window_id=3), payload)
    assert [c["label"] for c in observation.candidates] == ["Save"]
    assert "1" not in observation.target_map()


def test_execute_tolerates_a_result_lost_to_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A click that navigates can unload the page before the eval returns; that is
    not a click failure and must not abort the run."""
    from jev_use.choosers import Decision

    monkeypatch.setattr(browser, "_js", lambda *a: "")
    obs = browser.Observation(
        browser.Target(port=1, pid=2, window_id=3), {"url": "u", "elements": []}
    )
    browser.execute(  # must not raise
        None, browser.Target(port=1, pid=2, window_id=3),
        Decision(kind="click_element", element_id="0"), obs,
    )


def test_replay_aborts_when_a_description_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale plan must stop, not guess."""
    payload = {"url": "u", "elements": [{"kind": "a", "ref": 0, "text": "something else"}]}
    monkeypatch.setattr(browser, "snapshot", lambda *a, **k: browser.Observation(
        browser.Target(port=1, pid=2, window_id=3), payload
    ))
    call = {"driver": object()}
    result = browser.replay(
        call["driver"], browser.Target(port=1, pid=2, window_id=3), "g",
        [{"kind": "click_element", "target": 'a "Learn more"'}], act=True,
    )
    assert result.outcome == "replay_miss"
    assert "nothing described" in result.steps[0].note


def test_browser_use_exposes_the_decompose_switch() -> None:
    schema = {t["name"]: t for t in mcp_server.TOOLS}["browser_use"]["inputSchema"]["properties"]
    assert schema["decompose"]["default"] is True


def test_browser_use_points_at_browser_read_for_questions() -> None:
    """Routing detail that cost a real task: stepping is not reading."""
    tools = {t["name"]: t for t in mcp_server.TOOLS}
    assert "browser_read" in tools["browser_use"]["description"]


def test_defaults_are_single_sourced() -> None:
    schema = {t["name"]: t for t in mcp_server.TOOLS}["browser_use"]["inputSchema"]["properties"]
    assert schema["max_steps"]["default"] == mcp_server.DEFAULT_MAX_STEPS
    assert schema["min_confidence"]["default"] == mcp_server.DEFAULT_MIN_CONFIDENCE
    assert schema["settle"]["default"] == mcp_server.DEFAULT_SETTLE


def test_unknown_tool_is_an_error_result() -> None:
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert response["result"]["isError"] is True


def test_driver_refusal_returns_as_readable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is operational guidance, not a stack trace."""

    def refuse(_args):
        raise DriverError("No CDP endpoint found. Run scripts/enable-cdp.sh")

    monkeypatch.setitem(mcp_server.HANDLERS, "browser_use", refuse)
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "browser_use", "arguments": {"goal": "x"}},
        }
    )
    body = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert "enable-cdp.sh" in body
    assert "Traceback" not in body


def test_browser_profiles_reports_nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [])
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({"only_running": True})
    assert "none drivable" in text


def test_browser_profiles_names_the_route_when_no_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running Chrome cannot expose CDP on its default profile, so the answer is
    browser_open, not 'try again'."""
    profile = browser.Profile(pid=1, profile_dir="/home/h/.config/google-chrome", port=None)
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [profile])
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert "none drivable" in text
    assert "browser_open" in text
    assert "cdp=no" in text
    assert "not answering" not in text, "no port is not the same as a dead port"


def test_browser_profiles_lists_one_line_per_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several processes share one profile; that is one profile, not three."""
    shared = "/home/h/.config/google-chrome"
    profiles = [
        browser.Profile(pid=1, profile_dir=shared, port=None),
        browser.Profile(pid=2, profile_dir=shared, port=None),
        browser.Profile(pid=3, profile_dir=shared, port=None),
    ]
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: profiles)
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert text.count(shared) == 1


def test_browser_profiles_keeps_the_cdp_serving_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "/home/h/.config/google-chrome"
    profiles = [
        browser.Profile(pid=1, profile_dir=shared, port=None),
        browser.Profile(pid=2, profile_dir=shared, port=9222),
    ]
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: profiles)
    monkeypatch.setattr(mcp_server, "cdp_alive", lambda port, timeout=1.5: True)
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])
    text = mcp_server.tool_browser_profiles({})
    assert "pid=2" in text, "the process that actually serves CDP must be the one shown"
    assert "none drivable" not in text
