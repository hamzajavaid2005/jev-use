"""GoLogin: launched by GoLogin, not copied like a Chrome profile.

The thing that matters here is that we do *not* go anywhere near the Chrome
profile-copy path: a GoLogin profile is handed to the official SDK, which starts
Orbita with the profile's own fingerprint, and all we take back is the port.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from jev_use import gologin, mcp_server


# -- token + sdk ------------------------------------------------------------


def test_token_prefers_the_standard_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLOGIN_TOKEN", "  tok-a  ")
    monkeypatch.setenv("JEV_USE_GOLOGIN_TOKEN", "tok-b")
    assert gologin.token() == "tok-a"


def test_token_falls_back_to_the_prefixed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOLOGIN_TOKEN", raising=False)
    monkeypatch.setenv("JEV_USE_GOLOGIN_TOKEN", "tok-b")
    assert gologin.token() == "tok-b"


def test_no_token_is_none_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOLOGIN_TOKEN", raising=False)
    monkeypatch.delenv("JEV_USE_GOLOGIN_TOKEN", raising=False)
    assert gologin.token() is None


def test_listing_without_a_token_explains_how_to_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOLOGIN_TOKEN", raising=False)
    monkeypatch.delenv("JEV_USE_GOLOGIN_TOKEN", raising=False)
    with pytest.raises(gologin.GoLoginError) as excinfo:
        gologin.profiles()
    assert "--gologin-token" in str(excinfo.value)


# -- debugger address -------------------------------------------------------


def test_port_from_a_local_debugger_address() -> None:
    assert gologin.port_from_address("127.0.0.1:53142") == 53142


def test_port_from_a_websocket_debugger_address() -> None:
    assert gologin.port_from_address("ws://127.0.0.1:9222/devtools/browser/abc") == 9222


def test_a_cloud_address_without_a_port_is_refused_clearly() -> None:
    with pytest.raises(gologin.GoLoginError) as excinfo:
        gologin.port_from_address("wss://profile.orbita.gologin.com/devtools/browser/abc")
    assert "cloud" in str(excinfo.value)


# -- listing + matching -----------------------------------------------------


def test_profiles_accepts_the_bare_list_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gologin, "_api_get", lambda path: [{"id": "1", "name": "Acme"}, {"id": "2"}]
    )
    found = gologin.profiles()
    assert [(p.id, p.name) for p in found] == [("1", "Acme"), ("2", "2")]


def test_profiles_accepts_the_wrapped_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gologin, "_api_get", lambda path: {"profiles": [{"id": "9", "name": "X"}]})
    assert [p.id for p in gologin.profiles()] == ["9"]


def test_profiles_fetches_remaining_pages_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def api(path):
        calls.append(path)
        if path == "/browser/v2":
            return {"profiles": [{"id": str(i)} for i in range(30)]}
        return {"profiles": [{"id": "29"}, {"id": "30", "name": "Last profile"}]}
    monkeypatch.setattr(gologin, "_api_get", api)
    found = gologin.profiles()
    assert len(found) == 31
    assert found[0].name == "0"
    assert any(profile.name == "Last profile" for profile in found)
    assert calls == ["/browser/v2", "/browser/v2?page=2"]


def test_profiles_are_sorted_by_numeric_profile_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gologin,
        "_api_get",
        lambda path: {
            "profiles": [
                {"id": "22", "name": "Profile 22"},
                {"id": "1", "name": "Profile 1"},
                {"id": "10", "name": "Profile 10"},
            ]
        },
    )
    assert [profile.name for profile in gologin.profiles()] == [
        "Profile 1", "Profile 10", "Profile 22"
    ]


def test_profiles_refuses_repeated_full_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gologin, "_api_get", lambda path: [{"id": str(i)} for i in range(30)])
    with pytest.raises(gologin.GoLoginError, match="pagination repeated"):
        gologin.profiles()


def test_profiles_refuses_unexpected_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gologin, "_api_get", lambda path: {"error": "Invalid request"})
    with pytest.raises(gologin.GoLoginError, match="unexpected response shape"):
        gologin.profiles()


def test_find_matches_a_name_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gologin, "profiles", lambda: [gologin.GoLoginProfile("1", "Acme Ads")]
    )
    assert gologin.find("acme").id == "1"


def test_find_refuses_an_ambiguous_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gologin,
        "profiles",
        lambda: [gologin.GoLoginProfile("1", "Acme A"), gologin.GoLoginProfile("2", "Acme B")],
    )
    with pytest.raises(gologin.GoLoginError, match="several"):
        gologin.find("acme")


# -- launching --------------------------------------------------------------


class _FakeGoLogin:
    """Stands in for the SDK: records the options and returns a debugger address."""

    instances: list["_FakeGoLogin"] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.stopped = False
        _FakeGoLogin.instances.append(self)

    def start(self) -> str:
        # The real SDK prints; that must never reach stdout.
        print("DEBUG: starting orbita")
        return "127.0.0.1:53142"

    def stop(self) -> None:
        print("profile stopped")
        self.stopped = True


@pytest.fixture()
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_FakeGoLogin]:
    _FakeGoLogin.instances = []
    module = types.ModuleType("gologin")
    module.GoLogin = _FakeGoLogin
    monkeypatch.setitem(sys.modules, "gologin", module)
    monkeypatch.setenv("GOLOGIN_TOKEN", "tok")
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    return _FakeGoLogin


def test_launch_starts_the_profile_and_returns_the_port(
    fake_sdk: type[_FakeGoLogin], capsys: pytest.CaptureFixture[str]
) -> None:
    session = gologin.launch(gologin.GoLoginProfile("id-1", "Acme"))

    assert session.port == 53142
    assert session.debugger == "127.0.0.1:53142"
    assert fake_sdk.instances[0].options["profile_id"] == "id-1"
    assert fake_sdk.instances[0].options["token"] == "tok"
    # The SDK's prints must go to stderr; stdout is the JSON-RPC channel.
    assert capsys.readouterr().out == ""


def test_launch_opts_out_of_telemetry(fake_sdk: type[_FakeGoLogin], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    gologin.launch(gologin.GoLoginProfile("id-1", "Acme"))
    assert os.environ["DISABLE_TELEMETRY"] == "true", "the user did not ask to phone home"


def test_launch_passes_a_url_and_headless_as_extra_params(
    fake_sdk: type[_FakeGoLogin],
) -> None:
    gologin.launch(
        gologin.GoLoginProfile("id-1", "Acme"),
        url="https://example.com",
        headless=True,
    )
    extra = fake_sdk.instances[0].options["extra_params"]
    assert "--headless" in extra
    assert "https://example.com" in extra


def test_stop_stops_the_sdk_and_is_idempotent(
    fake_sdk: type[_FakeGoLogin], capsys: pytest.CaptureFixture[str]
) -> None:
    session = gologin.launch(gologin.GoLoginProfile("id-1", "Acme"))
    handle = fake_sdk.instances[0]

    session.stop()
    assert handle.stopped is True
    assert capsys.readouterr().out == "", "the SDK's stop message is not ours to print"

    session.stop()  # second call is a no-op, not an error


def test_launch_without_the_sdk_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOLOGIN_TOKEN", "tok")
    monkeypatch.setitem(sys.modules, "gologin", None)  # import raises
    monkeypatch.setattr(gologin, "sdk_installed", lambda: False)
    with pytest.raises(gologin.GoLoginError) as excinfo:
        gologin.launch(gologin.GoLoginProfile("id-1", "Acme"))
    assert "--gologin-token" in str(excinfo.value)


# -- how the tools use it ---------------------------------------------------


def test_browser_open_vendor_gologin_without_a_token_explains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOLOGIN_TOKEN", raising=False)
    monkeypatch.delenv("JEV_USE_GOLOGIN_TOKEN", raising=False)
    out = mcp_server.tool_browser_open({"profile": "Acme", "vendor": "gologin"})
    assert "--gologin-token" in out


def test_browser_open_auto_falls_back_to_gologin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Chrome profile by that name, a GoLogin token, and a matching GoLogin
    profile: it should launch GoLogin rather than fail."""
    monkeypatch.setenv("GOLOGIN_TOKEN", "tok")

    def no_chrome_profile(wanted: str):
        raise ValueError("no profile")

    monkeypatch.setattr(mcp_server, "find_profile", no_chrome_profile)
    monkeypatch.setattr(
        mcp_server.gologin, "find", lambda wanted: mcp_server.gologin.GoLoginProfile("id-1", "Acme")
    )
    stopped = {"port": None}

    class _Session:
        profile = mcp_server.gologin.GoLoginProfile("id-1", "Acme")
        port = 53142
        debugger = "127.0.0.1:53142"

        def stop(self) -> None:
            stopped["port"] = self.port

    monkeypatch.setattr(mcp_server.gologin, "launch", lambda *a, **k: _Session())
    monkeypatch.setattr(mcp_server, "cdp_alive", lambda port, timeout=1.5: False)

    try:
        out = mcp_server.tool_browser_open({"profile": "Acme"})
        assert "opened GoLogin profile" in out
        assert "53142" in out
        assert mcp_server._GOLOGIN["session"] is not None

        closed = mcp_server.tool_browser_close({})
        assert "stopped GoLogin profile" in closed
        assert stopped["port"] == 53142, "close must stop the SDK session"
        assert mcp_server._GOLOGIN["session"] is None
    finally:
        mcp_server._GOLOGIN["session"] = None


def test_browser_close_with_nothing_open_is_not_an_error() -> None:
    mcp_server._GOLOGIN["session"] = None
    assert "nothing to close" in mcp_server.tool_browser_close({})


def test_browser_profiles_says_how_to_enable_gologin_without_a_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("GOLOGIN_TOKEN", raising=False)
    monkeypatch.delenv("JEV_USE_GOLOGIN_TOKEN", raising=False)
    monkeypatch.setattr(mcp_server, "running_profiles", lambda: [])
    monkeypatch.setattr(mcp_server, "local_profiles", lambda: [])

    from jev_use import host

    # GoLogin present on disk (as a real user would have it) but no token yet.
    (tmp_path / "browser").mkdir()
    monkeypatch.setattr(host, "gologin_browser_root", lambda: tmp_path / "browser")

    text = mcp_server.tool_browser_profiles({})
    assert "GOLOGIN" in text
    assert "--gologin-token" in text
