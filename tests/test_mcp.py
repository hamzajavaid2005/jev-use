"""Protocol-level tests for the MCP server.

The tool-surface contract lives in `test_browser.py`, which owns the browser-only
surface. This file covers only the JSON-RPC plumbing, so the transport and the
product surface can change independently.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from jev_use import mcp_server


def test_initialize_reports_the_server_identity() -> None:
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert response["result"]["serverInfo"] == {
        "name": mcp_server.SERVER_NAME,
        "version": mcp_server.SERVER_VERSION,
    }
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert "tools" in response["result"]["capabilities"]


def test_notifications_produce_no_response() -> None:
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


@pytest.mark.parametrize("version", sorted(mcp_server.SUPPORTED_PROTOCOL_VERSIONS))
def test_initialize_negotiates_supported_client_version(version: str) -> None:
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": version}})
    assert response["result"]["protocolVersion"] == version


def test_initialize_unknown_version_offers_supported_version() -> None:
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "unknown"}})
    assert response["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_tools_list_returns_the_registry() -> None:
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    returned = [t["name"] for t in response["result"]["tools"]]
    assert returned == [t["name"] for t in mcp_server.TOOLS]


def test_unknown_method_is_reported() -> None:
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nonsense"})
    assert response["error"]["code"] == -32601


def test_ping() -> None:
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}


def test_a_request_without_an_id_gets_no_reply() -> None:
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "some/request"}) is None


def test_unexpected_exceptions_become_error_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashing tool must not take the server down."""

    def boom(_args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(mcp_server.HANDLERS, "browser_read", boom)
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "browser_read", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert "kaboom" in response["result"]["content"][0]["text"]


# -- prompts ----------------------------------------------------------------


def test_prompts_list_exposes_both_entry_points() -> None:
    """The slash-command entry points: /browser-use and /mobile-use in any harness."""
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "prompts/list"})
    names = [p["name"] for p in response["result"]["prompts"]]
    assert names == ["browser-use", "mobile-use"]
    assert all(p["arguments"][0]["required"] is True for p in response["result"]["prompts"])


def test_prompts_get_exposes_mobile_use() -> None:
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 12, "method": "prompts/get",
         "params": {"name": "mobile-use", "arguments": {"task": "turn on Airplane mode"}}}
    )
    text = response["result"]["messages"][0]["content"]["text"]
    assert "turn on Airplane mode" in text
    assert "android_devices" in text, "must send the model to device discovery first"
    assert "android_read" in text, "must name the step that actually answers"


def test_prompts_get_returns_instructions_containing_the_task() -> None:
    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "prompts/get",
            "params": {"name": "browser-use", "arguments": {"task": "check the usage page"}},
        }
    )
    text = response["result"]["messages"][0]["content"]["text"]
    assert "check the usage page" in text
    assert "browser_profiles" in text, "must send the model to discovery first"
    assert "browser_read" in text, "must name the step that actually answers"


def test_the_prompt_routes_to_browser_open_when_nothing_is_drivable() -> None:
    """The ten-turn spiral came from retrying an unrecoverable state.

    The fix is not 'stop' — it is 'call browser_open', which actually opens a
    profile from a copy. The prompt must name that tool.
    """
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "prompts/get",
         "params": {"name": "browser-use", "arguments": {"task": "x"}}}
    )
    text = response["result"]["messages"][0]["content"]["text"]
    assert "browser_open" in text
    assert "Never substitute a different browser" in text
    assert "explicitly open Profile 1 first" in text
    assert "never API/newest-first" in text


def test_unknown_prompt_is_an_error() -> None:
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 10, "method": "prompts/get", "params": {"name": "nope"}}
    )
    assert response["error"]["code"] == -32602


def test_initialize_advertises_prompts() -> None:
    response = mcp_server.handle(
        {"jsonrpc": "2.0", "id": 11, "method": "initialize", "params": {}}
    )
    assert "prompts" in response["result"]["capabilities"]


def test_handlers_cover_every_declared_tool() -> None:
    declared = {t["name"] for t in mcp_server.TOOLS}
    assert declared == set(mcp_server.HANDLERS)


# -- driver sessions --------------------------------------------------------


def test_a_healthy_driver_session_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """browser_use -> browser_read -> browser_extract must not each spawn a driver."""
    created: list[Any] = []
    monkeypatch.setenv("JEV_USE_TRANSPORT", "driver")

    class FakeDriver:
        def __init__(self) -> None:
            self._alive = True
            created.append(self)

        @property
        def alive(self) -> bool:
            return self._alive

        def start(self) -> None:
            pass

        def close(self) -> None:
            self._alive = False

    monkeypatch.setattr(mcp_server, "Driver", FakeDriver)
    monkeypatch.setattr(
        mcp_server,
        "attach_helpfully",
        lambda driver, profile=None, port=None: ("t", profile, port),
    )
    mcp_server.reset_browser_session()
    try:
        first, _ = mcp_server._browser_session(None)
        second, _ = mcp_server._browser_session(None)
        assert first is second, "a healthy session must be reused"
        assert len(created) == 1

        first.close()
        third, _ = mcp_server._browser_session(None)
        assert third is not first and len(created) == 2, "a dead child must be replaced"

        work, _ = mcp_server._browser_session("work")
        home, _ = mcp_server._browser_session("home")
        assert work is not home, "a different profile needs a different attach"

        # An explicit port is part of the session key too: same port reuses, a
        # different port re-attaches (GoLogin picks a new random port each launch).
        p1, t1 = mcp_server._browser_session(None, 53142)
        p2, t2 = mcp_server._browser_session(None, 53142)
        assert p1 is p2 and t1 == t2, "the same port is the same session"
        assert t2 == ("t", None, 53142), "the port reaches attach_helpfully"
        p3, _ = mcp_server._browser_session(None, 53143)
        assert p3 is not p1, "a different port needs a different attach"
    finally:
        mcp_server.reset_browser_session()


def test_load_env_does_not_clobber_an_existing_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / ".env").write_text("JEV_TEST_KEY=fromfile\n")
    monkeypatch.setattr(mcp_server, "__file__", str(tmp_path / "pkg" / "mcp_server.py"))
    monkeypatch.setenv("JEV_TEST_KEY", "fromenv")

    mcp_server.load_env()
    assert os.environ["JEV_TEST_KEY"] == "fromenv", "an explicit env var must win"


def test_load_env_survives_a_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_server, "__file__", str(tmp_path / "pkg" / "mcp_server.py"))
    mcp_server.load_env()


# -- the explicit port argument ---------------------------------------------
#
# A harness sends JSON, so `port` can arrive as a number or a string, and a
# missing one must read as "auto-detect" rather than 0 (port 0 is a real,
# meaninglessly-low value that would fail the /json/version probe with a
# confusing message).


def test_port_argument_is_parsed_leniently() -> None:
    assert mcp_server._port({}) is None
    assert mcp_server._port({"port": None}) is None
    assert mcp_server._port({"port": ""}) is None
    assert mcp_server._port({"port": 9222}) == 9222
    assert mcp_server._port({"port": "53142"}) == 53142
    assert mcp_server._port({"port": "not-a-port"}) is None
