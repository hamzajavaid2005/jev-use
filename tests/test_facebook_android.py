"""Exercise full account flows with a phone state machine, no live mutations."""
import html

import pytest

from jev_use import android, facebook_android as fb, facebook_audit


def node(label="", *, cls="android.widget.Button", bounds=(20, 200, 1000, 290), clickable=True, **attrs):
    attributes = {"class": cls, "text": label, "package": android.FACEBOOK_APP,
                  "bounds": f"[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]",
                  "clickable": str(clickable).lower(), "enabled": "true", **attrs}
    return "<node " + " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items()) + " />"


def screen(nodes):
    return android.Observation("S", android.parse_nodes("<hierarchy>" + "".join(nodes) + "</hierarchy>"),
                               (1080, 2160), android.FACEBOOK_APP + "/.Main")


class Phone:
    def __init__(self, monkeypatch, tabs=6):
        self.current = "Afiza Parween"
        self.names = ["Afiza Parween", "Tahir Shah", "Name, With Comma"]
        self.state = "feed"
        self.tabs = tabs
        self.actions = []
        self.tick = 0
        self.login_reads = 0
        self.freeze_login = False
        self.wrong_account = False
        self.after_account = None
        self.scrollable = False
        self.offset = 0
        self.visible_count = 2
        self.terminal = False
        self.ineffective_dismiss = False
        self.labeled_menu = False
        self.top_tabs = False
        self.feed_hidden = False
        self.reveal_fails = False
        self.duplicate_profile_text = False
        self.feed_scrolls = 0
        monkeypatch.setattr(android, "snapshot", self.snapshot)
        monkeypatch.setattr(android, "tap", self.tap)
        monkeypatch.setattr(android, "key", self.back)
        monkeypatch.setattr(android, "account_location", self.location)
        monkeypatch.setattr(android, "scroll_region", self.scroll_region)
        monkeypatch.setattr(android, "scroll", self.scroll_feed)
        monkeypatch.setattr(fb.time, "monotonic", lambda: self.tick)
        monkeypatch.setattr(fb.time, "sleep", lambda seconds: None)

    def observation(self):
        if self.state == "feed":
            if self.feed_hidden:
                if self.duplicate_profile_text:
                    profile = (
                        '<node class="android.view.ViewGroup" text="Go to profile" '
                        f'package="{android.FACEBOOK_APP}" clickable="true" enabled="true" bounds="[20,200][500,290]">'
                        f'<node class="android.widget.TextView" text="Go to profile" package="{android.FACEBOOK_APP}" '
                        'clickable="false" enabled="true" bounds="[30,210][490,280]" /></node>'
                    )
                else:
                    profile = node("Go to profile")
                nodes = [profile, node("What's on your mind?", bounds=(20, 300, 1000, 390))]
                if self.reveal_fails:
                    return screen(nodes)
                return screen(nodes)
            size = 1080 // self.tabs
            y1, y2 = (170, 280) if self.top_tabs else (1920, 2060)
            nodes = [node(cls="android.view.View", bounds=(i * size, y1, (i + 1) * size, y2),
                          **{"resource-id": "app:id/nav"}) for i in range(self.tabs)]
            if self.labeled_menu:
                nodes.append(node("Menu", bounds=(800, 1800, 1050, 1900)))
            return screen(nodes)
        if self.state == "saved_landing":
            nodes = [node("Settings", clickable=False), node("Facebook from Meta", clickable=False)]
            nodes += [node(name + ",  9+ notifications", cls="android.view.ViewGroup",
                           bounds=(20, 400 + i * 150, 1000, 500 + i * 150))
                      for i, name in enumerate(self.names)]
            nodes += [node("Use another profile"), node("Create new account")]
            return screen(nodes)
        if self.state == "menu":
            return screen([node(self.current + ", see your profile", clickable=False),
                           node("Open profile switcher")])
        if self.state == "switcher":
            return screen([node("Dismiss", bounds=(0, 0, 100, 100)), node(self.current, cls="android.view.ViewGroup"),
                           node("Other accountsRed dot with new notifications", bounds=(20, 600, 1000, 690))])
        if self.state == "accounts":
            nodes = [node("Dismiss", bounds=(0, 0, 100, 100)), node("Other accounts", clickable=False)]
            visible = self.names[self.offset:self.offset + self.visible_count] if self.scrollable else self.names
            nodes += [node(name + (", 19 notifications" if name != self.current else ""), cls="android.view.ViewGroup",
                           bounds=(20, 400 + i * 150, 1000, 500 + i * 150)) for i, name in enumerate(visible)]
            if self.scrollable:
                nodes.append(node(clickable=False, scrollable="true", bounds=(0, 300, 1080, 2000)))
                if self.terminal and self.offset + self.visible_count >= len(self.names):
                    nodes.append(node("Log into another account", clickable=False))
            return screen(nodes)
        if self.state == "logging":
            return screen([node("Logging in as " + self.current + "…", clickable=False)])
        if self.state == "location":
            return screen([node("Your Primary Location", clickable=False)])
        return screen([node(self.state, clickable=False)])

    def snapshot(self, serial):
        self.tick += 1
        if self.state == "logging" and not self.freeze_login:
            self.login_reads += 1
            if self.login_reads >= 3:
                self.state = "feed"
        return self.observation()

    def tap(self, serial, x, y):
        observation = self.observation()
        targets = [target for target in observation.targets if target["centred"] == (x, y)]
        assert len(targets) == 1
        label = targets[0]["label"]
        self.actions.append(label or "menu-tab")
        if self.state == "feed":
            self.state = "menu"
        elif label == "Dismiss":
            self.state = "switcher" if self.ineffective_dismiss and self.state == "accounts" else "menu"
        elif self.state == "saved_landing":
            self.current = fb.account_name(label)
            self.state = "logging"
        elif label == "Open profile switcher":
            self.state = "switcher"
        elif label.startswith("Other accounts"):
            self.state = "accounts"
        else:
            if not self.wrong_account:
                self.current = fb.account_name(label)
            self.state = "logging"

    def back(self, serial, code):
        assert code == android.KEYCODES["go_back"]
        self.actions.append("back")
        if self.after_account:
            self.current = self.after_account
        self.state = "menu"

    def scroll_region(self, serial, bounds, direction):
        assert direction == "scroll_down"
        before = self.offset
        self.offset = min(max(0, len(self.names) - self.visible_count), self.offset + 1)
        self.actions.append("scroll")

    def scroll_feed(self, serial, size, direction):
        assert direction == "scroll_up"
        self.feed_scrolls += 1
        self.actions.append("feed-scroll-up")
        if not self.reveal_fails:
            self.feed_hidden = False

    def location(self, serial, timeout):
        self.actions.append("location-read")
        self.state = "location"
        return android.AccountLocation(serial, "fresh-url", "Lahore, Punjab 54", "location")


@pytest.mark.parametrize("tabs", [5, 6])
def test_lists_observed_accounts_without_a_model(monkeypatch, tabs):
    phone = Phone(monkeypatch, tabs)
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert result["accounts"] == phone.names
    assert result["active_account"] == "Afiza Parween"
    assert phone.actions == ["menu-tab", "Open profile switcher", "Other accountsRed dot with new notifications", "Dismiss"]


def test_switches_once_waits_for_login_and_verifies_location_identity(monkeypatch):
    phone = Phone(monkeypatch)
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == "location"
    assert result["account_attempted"] is True
    assert result["account"] == "Tahir Shah"
    assert result["location"] == "Lahore, Punjab 54"
    assert result["identity_verified"] is True
    assert phone.actions.count("Tahir Shah, 19 notifications") == 1
    assert phone.login_reads == 3
    assert phone.state == "menu"
    assert result["timings"]["hierarchy_reads"] > 0
    assert result["timings"]["location_open_and_poll"] >= 0


def test_restart_recovers_frozen_login_without_reselecting_account(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "logging"
    phone.freeze_login = True
    restarts = []

    def restart(serial, **kwargs):
        restarts.append(serial)
        phone.state = "feed"
        phone.freeze_login = False

    monkeypatch.setattr(android, "restart_facebook", restart)
    result = fb.run("S", action="restart", account="Afiza Parween", timeout=50)
    assert restarts == ["S"]
    assert result["state"] == "location"
    assert result["identity_verified"] is True
    assert result["recovery_attempts"] == 1
    assert "Open profile switcher" not in phone.actions
    assert phone.actions.count("location-read") == 1


def test_restart_does_not_attribute_location_to_different_active_account(monkeypatch):
    phone = Phone(monkeypatch)
    phone.current = "Tahir Shah"
    monkeypatch.setattr(android, "restart_facebook", lambda *args, **kwargs: None)
    result = fb.run("S", action="restart", account="Afiza Parween", timeout=50)
    assert result["state"] == "identity_mismatch"
    assert "location" not in result
    assert "location-read" not in phone.actions
    assert "Open profile switcher" not in phone.actions


def test_current_location_resumes_verified_active_account_without_selection(monkeypatch):
    phone = Phone(monkeypatch)
    result = fb.run("S", action="current_location", account="Afiza Parween", timeout=50)
    assert result["state"] == "location"
    assert result["account"] == "Afiza Parween"
    assert result["identity_verified"] is True
    assert result["account_attempted"] is True
    assert "Open profile switcher" not in phone.actions
    assert not any(action.endswith("notifications") for action in phone.actions)
    assert phone.actions.count("location-read") == 1


def test_current_location_waits_for_matching_pending_login_without_reselection(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "logging"
    result = fb.run("S", action="current_location", account="Afiza Parween", timeout=50)
    assert result["state"] == "location"
    assert result["account_attempted"] is True
    assert phone.login_reads == 3
    assert phone.actions.count("location-read") == 1
    assert "Open profile switcher" not in phone.actions
    assert not any(action.endswith("notifications") for action in phone.actions)


def test_current_location_times_out_on_frozen_matching_login_without_taps(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "logging"
    phone.freeze_login = True
    result = fb.run("S", action="current_location", account="Afiza Parween", timeout=10)
    assert result["state"] == "timeout"
    assert result["account_attempted"] is False
    assert phone.actions == []


def test_current_location_rejects_different_pending_login_without_taps(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "logging"
    phone.current = "Tahir Shah"
    result = fb.run("S", action="current_location", account="Afiza Parween", timeout=50)
    assert result["state"] == "identity_mismatch"
    assert phone.actions == []


def test_current_location_identity_mismatch_does_not_read_or_select(monkeypatch):
    phone = Phone(monkeypatch)
    phone.current = "Tahir Shah"
    result = fb.run("S", action="current_location", account="Afiza Parween", timeout=50)
    assert result["state"] == "identity_mismatch"
    assert "location-read" not in phone.actions
    assert "Open profile switcher" not in phone.actions
    assert not any(action.endswith("notifications") for action in phone.actions)
    assert result["account_attempted"] is False


def test_current_account_skips_switching_and_resumes_from_open_picker(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "accounts"
    result = fb.run("S", action="location", account="Afiza Parween")
    assert result["state"] == "location"
    assert result["account_attempted"] is True
    assert phone.actions == ["Dismiss", "location-read", "back"]


def test_timeout_never_repeats_account_selection(monkeypatch):
    phone = Phone(monkeypatch)
    phone.freeze_login = True
    result = fb.run("S", action="location", account="Tahir Shah", timeout=10)
    assert result["state"] == "timeout"
    assert phone.actions.count("Tahir Shah, 19 notifications") == 1
    assert "location-read" not in phone.actions
    assert "location" not in result


@pytest.mark.parametrize("after_read", [False, True])
def test_does_not_attribute_location_to_wrong_account(monkeypatch, after_read):
    phone = Phone(monkeypatch)
    phone.wrong_account = not after_read
    phone.after_account = "Someone Else" if after_read else None
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == "identity_mismatch"
    assert "location" not in result
    assert "identity_verified" not in result
    assert ("location-read" in phone.actions) == after_read


@pytest.mark.parametrize("message,state", [("Unlock, Use fingerprint to unlock", "locked"),
                                          ("Connection lost, Tap to retry", "network_error"),
                                          ("Log into Facebook", "login")])
def test_blockers_stop_without_taps(monkeypatch, message, state):
    phone = Phone(monkeypatch)
    phone.state = message
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == state
    assert phone.actions == []


def test_unknown_nav_layout_does_not_guess_menu(monkeypatch):
    phone = Phone(monkeypatch, tabs=4)
    result = fb.run("S", action="accounts")
    assert result["state"] == "unsupported_ui"
    assert phone.actions == []


def test_explicit_menu_label_supports_observed_four_tab_variant(monkeypatch):
    phone = Phone(monkeypatch, tabs=4)
    phone.labeled_menu = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert phone.actions[0] == "Menu"


def test_observed_top_six_tab_layout_opens_menu_and_verifies_identity(monkeypatch):
    phone = Phone(monkeypatch, tabs=6)
    phone.top_tabs = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert phone.actions[0] == "menu-tab"


def test_hidden_feed_navigation_gets_one_verified_upward_scroll(monkeypatch):
    phone = Phone(monkeypatch)
    phone.feed_hidden = True
    phone.duplicate_profile_text = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert phone.feed_scrolls == 1
    assert phone.actions[:2] == ["feed-scroll-up", "menu-tab"]


def test_hidden_navigation_scroll_requires_both_observed_feed_markers(monkeypatch):
    phone = Phone(monkeypatch, tabs=4)
    phone.feed_hidden = True
    # Remove one feed marker while retaining the unsupported four-item layout.
    original = phone.observation

    def without_composer():
        if phone.state == "feed" and phone.feed_hidden:
            return screen([node("Go to profile")])
        return original()

    phone.observation = without_composer
    result = fb.run("S", action="accounts")
    assert result["state"] == "unsupported_ui"
    assert phone.feed_scrolls == 0


def test_hidden_feed_scroll_is_attempted_once_when_nav_stays_hidden(monkeypatch):
    phone = Phone(monkeypatch)
    phone.feed_hidden = True
    phone.reveal_fails = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "unsupported_ui"
    assert phone.feed_scrolls == 1
    assert phone.actions == ["feed-scroll-up"]


def test_ineffective_picker_dismiss_recovers_from_observed_switcher(monkeypatch):
    phone = Phone(monkeypatch)
    phone.ineffective_dismiss = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert phone.actions[-2:] == ["Dismiss", "back"]


@pytest.mark.parametrize("problem,state", [("missing", "account_not_found"), ("duplicate", "account_ambiguous")])
def test_incomplete_or_ambiguous_picker_is_not_claimed_complete(monkeypatch, problem, state):
    phone = Phone(monkeypatch)
    if problem == "duplicate":
        phone.names.append("Tahir Shah")
    result = fb.run("S", action="location", account="Missing" if problem == "missing" else "Tahir Shah")
    assert result["state"] == state
    assert "location-read" not in phone.actions


def test_exact_selection_searches_scrolling_picker_without_full_enumeration(monkeypatch):
    phone = Phone(monkeypatch)
    phone.names = ["Afiza Parween", "Tahir Shah", "Name, With Comma", "Far Account"]
    phone.scrollable = True
    phone.visible_count = 1
    result = fb.run("S", action="location", account="Far Account")
    assert result["state"] == "location"
    assert phone.actions.count("Far Account, 19 notifications") == 1
    assert phone.actions.count("scroll") == 3


def test_scrollable_enumeration_requires_observed_end_and_restores_menu(monkeypatch):
    phone = Phone(monkeypatch)
    phone.names = ["Afiza Parween", "Tahir Shah", "Name, With Comma", "Far Account"]
    phone.scrollable = True
    phone.visible_count = 2
    phone.terminal = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "accounts"
    assert result["accounts"] == phone.names
    assert phone.actions[-1] == "Dismiss"


def test_scrollable_enumeration_without_end_marker_is_incomplete(monkeypatch):
    phone = Phone(monkeypatch)
    phone.scrollable = True
    result = fb.run("S", action="accounts")
    assert result["state"] == "unsupported_ui"
    assert "completeness is unknown" in result["detail"]


def test_account_names_preserve_commas_and_strip_only_notification_suffixes():
    assert fb.account_name("Name, With Comma, 19 notifications") == "Name, With Comma"
    assert fb.account_name("Name, With Comma") == "Name, With Comma"


def test_identity_requires_menu_control_not_just_profile_like_post_text():
    observation = screen([node('Someone Else, see your profile', clickable=False)])
    assert fb.identity(observation) is None


def test_account_name_strips_plus_notification_suffix():
    assert fb.account_name("Facebook from Meta, 9+ notifications") == "Facebook from Meta"


@pytest.mark.parametrize("ending", ["…", "..."])
def test_menu_reports_observed_pending_login_immediately(monkeypatch, ending):
    phone = Phone(monkeypatch)
    phone.state = "Logging in as Tahir Shah" + ending
    result = fb.run("S", action="ready")
    assert result["state"] == "pending_login"
    assert "Tahir Shah" in result["detail"]
    assert result["pending_account"] == "Tahir Shah"
    assert result["account_attempted"] is False
    assert phone.actions == []


def test_expired_session_is_distinguished_from_generic_login(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "Session expired\nPlease log in again.\nOK"
    result = fb.run("S", action="ready")
    assert result["state"] == "session_expired"
    assert result["account_attempted"] is False
    assert phone.actions == []


def test_location_action_returns_expired_session_immediately(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "Session expired\nPlease log in again.\nOK"
    result = fb.run("S", action="location", account="Tahir Shah")
    assert result["state"] == "session_expired"
    assert result["account_attempted"] is False
    assert phone.actions == []


def test_saved_account_login_landing_is_not_reported_ready(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    result = fb.run("S", action="ready")
    assert result["state"] == "saved_accounts"
    assert result["account_attempted"] is False
    assert phone.actions == []


def test_accounts_bootstraps_one_exact_saved_card_then_enumerates_picker(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    result = fb.run("S", action="accounts", timeout=50)
    assert result["state"] == "accounts"
    assert result["active_account"] == "Afiza Parween"
    assert result["accounts"] == phone.names
    assert result["bootstrap_attempted"] is True
    assert result["bootstrap_account"] == "Afiza Parween"
    assert result["account_attempted"] is False
    assert phone.actions.count("Afiza Parween,  9+ notifications") == 1


def test_location_from_saved_landing_selects_requested_card_once_and_verifies(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    result = fb.run("S", action="location", account="Tahir Shah", timeout=50)
    assert result["state"] == "location"
    assert result["account"] == "Tahir Shah"
    assert result["identity_verified"] is True
    assert result["account_attempted"] is True
    assert result["bootstrap_account"] == "Tahir Shah"
    assert phone.actions.count("Tahir Shah,  9+ notifications") == 1


def test_saved_landing_duplicate_card_is_ambiguous_without_a_tap(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    phone.names.append("Afiza Parween")
    result = fb.run("S", action="accounts", timeout=50)
    assert result["state"] == "account_ambiguous"
    assert result["bootstrap_attempted"] is False
    assert phone.actions == []


def test_saved_landing_auth_prompt_after_one_selection_stops_without_retry(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    original_snapshot = phone.snapshot

    def snapshot(serial):
        if phone.state == "logging":
            phone.login_reads += 1
            if phone.login_reads >= 2:
                phone.state = "Enter password"
        if phone.state == "Enter password":
            return screen([node("Enter password", cls="android.widget.EditText", password="true")])
        return original_snapshot(serial)

    monkeypatch.setattr(android, "snapshot", snapshot)
    result = fb.run("S", action="accounts", timeout=50)
    assert result["state"] == "authentication_required"
    assert result["bootstrap_attempted"] is True
    assert result["bootstrap_account"] == "Afiza Parween"
    assert "pending_account" not in result
    assert phone.actions.count("Afiza Parween,  9+ notifications") == 1


def test_saved_landing_frozen_spinner_times_out_without_replaying_card(monkeypatch):
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    phone.freeze_login = True
    result = fb.run("S", action="accounts", timeout=3)
    assert result["state"] == "timeout"
    assert result["bootstrap_attempted"] is True
    assert result["bootstrap_account"] == "Afiza Parween"
    assert result["pending_account"] == "Afiza Parween"
    assert phone.actions.count("Afiza Parween,  9+ notifications") == 1


def test_audit_bootstraps_saved_landing_and_records_verified_current_account(tmp_path, monkeypatch):
    monkeypatch.setattr(facebook_audit.host, "work_root", lambda: tmp_path)
    phone = Phone(monkeypatch)
    phone.state = "saved_landing"
    outcomes = []

    def run_and_capture(serial, **kwargs):
        outcome = fb.run(serial, **kwargs)
        outcomes.append(outcome)
        return outcome

    result = facebook_audit.run("S", timeout=30, chunk_size=1,
                                chunk_budget_seconds=55, resume_token=None,
                                facebook_run=run_and_capture)
    assert result["state"] == "in_progress"
    assert result["results"][0]["account"] == "Afiza Parween"
    assert result["results"][0]["state"] == "location"
    assert result["results"][0]["identity_verified"] is True
    assert result.get("blocker") is None
    location_outcome = next(outcome for outcome in outcomes if outcome["action"] == "location")
    assert location_outcome["account_attempted"] is True
    assert phone.actions.count("Afiza Parween,  9+ notifications") == 1


def test_ready_is_read_only_for_recognized_feed_and_menu(monkeypatch):
    phone = Phone(monkeypatch)
    result = fb.run("S", action="ready")
    assert result["state"] == "ready"
    assert result["screen"] == "feed"
    assert phone.actions == []

    phone.state = "menu"
    result = fb.run("S", action="ready")
    assert result["state"] == "ready"
    assert result["screen"] == "menu"
    assert result["active_account"] == "Afiza Parween"
    assert phone.actions == []


def test_top_tab_bar_with_mixed_accessibility_labels_is_recognized():
    labels = ["", "Reels, tab 2 of 6", "Friends, tab 3 of 6", "",
              "Notifications, tab 5 of 6, 10 or more new", ""]
    observed = screen([node(label, cls="android.view.View",
                            bounds=(i * 180, 170, (i + 1) * 180, 280),
                            **{"resource-id": "app:id/nav"})
                       for i, label in enumerate(labels)])
    assert fb.menu_target(observed)["centred"] == (990, 225)


def test_password_prompt_stops_without_filling_or_selecting(monkeypatch):
    phone = Phone(monkeypatch)
    monkeypatch.setattr(android, 'snapshot', lambda serial: screen([
        node('Enter password', cls='android.widget.EditText', password='true')]))
    result = fb.run('S', action='location', account='Tahir Shah')
    assert result['state'] == 'authentication_required'
    assert phone.actions == []
