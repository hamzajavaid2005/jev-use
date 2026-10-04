"""End-to-end Facebook flow simulation using real parsed Android observations."""
from __future__ import annotations

import html

import pytest

from jev_use import android, facebook_android, facebook_flow, facebook_flow_state


ACCOUNT = "Shyam Desai"
PLACE = "Manila, Philippines"


_row_index = 0


def _node(label="", *, cls="android.widget.Button", bounds=None,
          clickable=True, checked=False, checkable=False, enabled=True,
          scrollable=False, package=None, content_desc=""):
    package = package or android.FACEBOOK_APP
    global _row_index
    if bounds is None:
        top = 200 + _row_index * 100
        bounds = (20, top, 1000, top + 80)
        _row_index += 1
    attrs = {
        "class": cls,
        "text": label,
        "content-desc": content_desc,
        "package": package,
        "bounds": f"[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]",
        "clickable": str(clickable).lower(),
        "enabled": str(enabled).lower(),
        "checked": str(checked).lower(),
        "checkable": str(checkable).lower(),
        "scrollable": str(scrollable).lower(),
    }
    return "<node " + " ".join(
        f'{key}="{html.escape(value, quote=True)}"' for key, value in attrs.items()
    ) + " />"


def _tab_row():
    names = ("Home", "Watch", "Marketplace", "Groups", "Notifications", "Menu")
    nodes = []
    for index, name in enumerate(names):
        left, right = index * 180, (index + 1) * 180
        nodes.append(_node(f"{name}, tab {index + 1} of 6", cls="android.view.View",
                           bounds=(left, 1920, right, 2060)))
    return nodes


class SimulatedPhone:
    def __init__(self):
        self.state = "saved_landing"
        self.login_reads = 0
        self.taps: list[str] = []
        self.scrolls: list[str] = []
        self.typed: list[str] = []
        self.posts = 0
        self.wrong_identity = False
        self.include_prefix_impostor = True

    def observation(self):
        global _row_index
        _row_index = 0
        nodes = []
        if self.state == "saved_landing":
            nodes += [_node("Facebook from Meta", clickable=False),
                      _node("Use another profile", clickable=False),
                      _node("Create new account", clickable=False),
                      _node(f"{ACCOUNT}, 9+ notifications", cls="android.view.ViewGroup",
                            bounds=(20, 400, 1000, 520))]
        elif self.state == "logging":
            nodes += [_node(f"Logging in as {ACCOUNT}…", clickable=False)]
        elif self.state == "feed":
            nodes += _tab_row()
        elif self.state == "feed_hidden":
            nodes += [_node("Go to profile", cls="android.view.ViewGroup",
                            bounds=(20, 220, 1000, 310))]
        elif self.state == "feed_visible":
            nodes += _tab_row()
            nodes += [_node("Go to profile", cls="android.view.ViewGroup",
                            bounds=(20, 220, 1000, 310)),
                      _node("What's on your mind?", bounds=(20, 340, 1000, 430))]
        elif self.state in ("menu", "menu_scrolled"):
            active = "Someone Else" if self.wrong_identity else ACCOUNT
            nodes += [_node("Open profile switcher"),
                      _node(f"{active}, see your profile", clickable=False)]
            nodes += _tab_row()
            if self.state == "menu_scrolled":
                nodes += [_node("Log out", bounds=(20, 1700, 1000, 1800))]
            else:
                nodes += [_node(cls="android.widget.ScrollView", clickable=False,
                                scrollable=True, bounds=(0, 250, 1080, 2050))]
        elif self.state == "composer":
            nodes += [_node("New post", clickable=False),
                      _node(ACCOUNT, clickable=False),
                      _node("Location. 3 of 4. Press to add a location tag to your post")]
        elif self.state == "picker":
            nodes += [_node("Add location", clickable=False),
                      _node("", cls="android.widget.EditText", clickable=True,
                            content_desc="Search", bounds=(20, 250, 1000, 340))]
            if self.include_prefix_impostor:
                nodes += [_node("Manila, Philippines, Manila, Philippines West, 17M check-ins, Manila, Philippines, 17",
                                cls="android.view.ViewGroup", bounds=(20, 420, 1000, 520))]
            nodes += [_node("Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, Philippines, 17",
                            cls="android.view.ViewGroup", bounds=(20, 540, 1000, 650))]
            nodes += [_node("More actions", bounds=(900, 540, 1070, 640))]
        elif self.state == "composer_tagged":
            nodes += [_node("New post", clickable=False), _node(ACCOUNT, clickable=False),
                      _node("Location. 3 of 4. Current location tag added to your post: Manila, Philippines. Press to edit or remove location tag"),
                      _node("Next", bounds=(800, 1900, 1060, 2040))]
        elif self.state in ("preview_friends", "preview_public"):
            audience = "Public" if self.state == "preview_public" else "Friends"
            nodes += [_node("New post", clickable=False),
                      _node(f"{ACCOUNT} is at {PLACE}., Just now, Map, {PLACE}"),
                      _node(f"Post audience, {audience}"),
                      _node("Post", bounds=(800, 1900, 1060, 2040))]
        elif self.state in ("audience_picker", "audience_picker_public"):
            selected_public = self.state == "audience_picker_public"
            nodes += [_node("Who can see your post?", clickable=False),
                      _node("Friends", cls="android.widget.RadioButton", checkable=True,
                            checked=not selected_public, bounds=(20, 500, 1000, 580)),
                      _node("Public, Anyone on or off Facebook", cls="android.widget.RadioButton",
                            checkable=True, checked=selected_public,
                            bounds=(20, 600, 1000, 680)),
                      _node("Set as default", cls="android.widget.CheckBox", checkable=True,
                            checked=False, bounds=(20, 700, 1000, 780)),
                      _node("Done", bounds=(800, 1800, 1060, 1900))]
        elif self.state == "feed_posted":
            nodes += _tab_row()
            nodes += [_node(f"{ACCOUNT} is in {PLACE}., Just now, Map, {PLACE}, Shared with: Public",
                            cls="android.view.ViewGroup", bounds=(20, 400, 1000, 850))]
        elif self.state == "logout_dialog":
            nodes += [_node("Log out of your account?", clickable=False),
                      _node("LOG OUT", bounds=(500, 1000, 1000, 1100))]
        return android.Observation(
            "simulated", android.parse_nodes("<hierarchy>" + "".join(nodes) + "</hierarchy>"),
            (1080, 2160), android.FACEBOOK_APP + "/.Main")

    def snapshot(self, serial):
        assert serial == "simulated"
        if self.state == "logging":
            if self.login_reads:
                self.state = "feed"
            else:
                self.login_reads += 1
        return self.observation()

    def tap(self, serial, x, y):
        observation = self.observation()
        targets = [target for target in observation.targets
                   if target["centred"] == (x, y)]
        assert len(targets) == 1, (self.state, (x, y), targets)
        target = targets[0]
        label = target["label"]
        self.taps.append(label)
        if self.state == "saved_landing":
            self.state = "logging"
            self.login_reads = 0
        elif self.state in ("feed", "feed_posted") and label == "Menu, tab 6 of 6":
            self.state = "menu"
        elif self.state == "menu" and label == "Home, tab 1 of 6":
            self.state = "feed_hidden"
        elif self.state == "feed_visible" and label == "What's on your mind?":
            self.state = "composer"
        elif self.state == "composer" and label.startswith("Location. 3 of 4. Press"):
            self.state = "picker"
        elif self.state == "picker" and label.startswith(PLACE + ", " + PLACE + ", "):
            self.state = "composer_tagged"
        elif self.state == "composer_tagged" and label == "Next":
            self.state = "preview_friends"
        elif self.state == "preview_friends" and label == "Post audience, Friends":
            self.state = "audience_picker"
        elif self.state == "audience_picker" and label == "Public, Anyone on or off Facebook":
            self.state = "audience_picker_public"
        elif self.state == "audience_picker_public" and label == "Done":
            self.state = "preview_public"
        elif self.state == "preview_public" and label == "Post":
            self.posts += 1
            self.state = "feed_posted"
        elif self.state == "menu_scrolled" and label == "Log out":
            self.state = "logout_dialog"
        elif self.state == "logout_dialog" and label == "LOG OUT":
            self.state = "saved_landing"
        else:
            raise AssertionError(f"unexpected tap {label!r} on {self.state}")

    def scroll(self, serial, screen, direction):
        self.scrolls.append(direction)
        if self.state == "feed_hidden" and direction == "scroll_up":
            self.state = "feed_visible"
        elif self.state == "menu" and direction == "scroll_down":
            self.state = "menu_scrolled"
        else:
            raise AssertionError(f"unexpected scroll {direction} on {self.state}")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.001
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def simulated_phone(tmp_path, monkeypatch):
    phone = SimulatedPhone()
    clock = FakeClock()
    monkeypatch.setattr(facebook_flow_state.host, "work_root", lambda: tmp_path)
    monkeypatch.setattr(android, "snapshot", phone.snapshot)
    monkeypatch.setattr(android, "tap", phone.tap)
    monkeypatch.setattr(android, "scroll", phone.scroll)
    monkeypatch.setattr(android, "type_into", lambda serial, text: phone.typed.append(text))
    monkeypatch.setattr(android, "account_location", lambda serial, **kwargs:
                        android.AccountLocation(serial=serial, url="fb://location",
                                                location="Muridke, Punjab 39", state="location",
                                                seconds=2.0, timings={"hierarchy_reads": 1.0}))
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    monkeypatch.setattr(android, "run", lambda *a, **k: pytest.fail("the decision-model loop must not run"))
    yield phone, clock


def test_one_model_free_call_runs_saved_login_location_public_post_and_verified_logout(simulated_phone):
    phone, clock = simulated_phone
    result = facebook_flow.run("simulated", ACCOUNT, place=PLACE, audience="Public",
                               timeout=55, publish=True, run_id="simulated-public-manila")

    assert result["state"] == "complete", (result.get("detail"), result, phone.state, phone.taps, phone.scrolls)
    assert result["stage"] == "logged_out"
    assert result["complete"] is True
    assert result["primary_location"] == "Muridke, Punjab 39"
    assert result["primary_location_state"] == "location"
    assert result["post_verified"] is True
    assert result["logout_verified"] is True
    assert phone.posts == 1
    assert phone.state == "saved_landing"
    assert phone.typed == []  # The exact recent Manila card was observed.
    assert [label for label in phone.taps if label.startswith(PLACE + ", " + PLACE + ", ")] == [
        "Manila, Philippines, Manila, Philippines, 17M check-ins, Manila, Philippines, 17"
    ]
    assert phone.scrolls == ["scroll_up", "scroll_down"]
    assert "Set as default" not in phone.taps
    assert phone.taps.count("Post") == 1
    assert phone.taps[-1] == "LOG OUT"
    assert clock.now < 55


def test_wrong_active_identity_stops_before_location_or_post(simulated_phone):
    phone, _ = simulated_phone
    phone.wrong_identity = True
    result = facebook_flow.run("simulated", ACCOUNT, place=PLACE, audience="Public",
                               timeout=55, publish=True, run_id="wrong-identity")
    assert result["state"] == "identity_mismatch"
    assert result["stage"] == "logging_in"
    assert phone.posts == 0
    assert "Post" not in phone.taps
