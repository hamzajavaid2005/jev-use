"""Subprocess coverage for the Python console script's direct-tool fallback."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_python_module(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "jev_use", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_python_module_call_help_uses_tool_cli_without_phone_access():
    result = run_python_module("call", "android_facebook", "--help")

    assert result.returncode == 0
    assert "--action" in result.stdout
    assert "--chunk-size" in result.stdout
    assert "--resume-token" in result.stdout
    assert "--continue-after-blocker" in result.stdout
    assert result.stderr == ""


def test_python_module_call_flow_help_exposes_place_audience_run_id_and_publish():
    result = run_python_module("call", "android_facebook_flow", "--help")
    assert result.returncode == 0
    assert "--account" in result.stdout
    assert "--place" in result.stdout
    assert "--audience" in result.stdout
    assert "--publish" in result.stdout
    assert "--run-id" in result.stdout
    assert "--resume-token" in result.stdout


def test_tool_cli_maps_facebook_flow_flags_to_handler_arguments(monkeypatch, capsys):
    from jev_use import tool_cli

    monkeypatch.setattr(tool_cli.mcp_server, "load_env", lambda: None)
    calls = []

    def handle(request):
        calls.append(request)
        return {"result": {"content": [{"type": "text", "text": '{"stage":"composing"}'}]}}

    monkeypatch.setattr(tool_cli.mcp_server, "handle", handle)
    assert tool_cli.main(["android_facebook_flow", "--account", "Shyam Desai",
                          "--serial", "device-1", "--place", "Manila, Philippines",
                          "--audience", "Public", "--publish", "--run-id", "job-123"]) == 0
    assert capsys.readouterr().out == '{"stage":"composing"}\n'
    request = calls[0]
    assert request["params"]["name"] == "android_facebook_flow"
    assert request["params"]["arguments"] == {
        "account": "Shyam Desai", "serial": "device-1",
        "place": "Manila, Philippines", "audience": "Public",
        "publish": True, "run_id": "job-123",
    }


def test_python_module_call_validates_required_arguments_before_device_access():
    result = run_python_module("call", "android_facebook")

    assert result.returncode == 2
    assert "missing arguments: action" in result.stderr
    assert "adb" not in result.stderr.lower()


def test_python_module_keeps_legacy_goal_cli():
    result = run_python_module("--help")

    assert result.returncode == 0
    assert "what to accomplish" in result.stdout
    assert "tool_cli" not in result.stdout


def test_tool_cli_returns_failure_for_mcp_error(monkeypatch, capsys):
    from jev_use import tool_cli

    monkeypatch.setattr(tool_cli.mcp_server, "load_env", lambda: None)
    monkeypatch.setattr(tool_cli.mcp_server, "handle", lambda request: {
        "result": {"isError": True, "content": [{"type": "text", "text": "invalid argument"}]}
    })

    assert tool_cli.main(["android_devices"]) == 1
    assert capsys.readouterr().out == "invalid argument\n"
