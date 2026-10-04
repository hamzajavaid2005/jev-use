"""Exercise the Android MCP entry point and serialized request behavior."""
import json
import threading
from types import SimpleNamespace

import pytest

from jev_use import android, mcp_server


@pytest.mark.parametrize('options,steps,settle', [({}, mcp_server.DEFAULT_MAX_STEPS, android.DEFAULT_SETTLE), ({'max_steps': 3, 'settle': 0.2}, 3, 0.2)])
def test_android_mcp_passes_validated_options(monkeypatch, options, steps, settle):
    calls = []
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='fixture-device'))
    monkeypatch.setattr(mcp_server, 'JevChooser', lambda: object())
    monkeypatch.setattr(mcp_server, 'TextModel', lambda: object())
    def run(serial, goal, chooser, **kwargs):
        calls.append(kwargs)
        return android.Result(goal=goal)
    monkeypatch.setattr(android, 'run', run)
    response = mcp_server.handle({'jsonrpc':'2.0', 'id':1, 'method':'tools/call', 'params':{'name':'android_use','arguments':{'goal':'open account menu', 'act':True, 'use_cache':False, **options}}})
    assert not response['result'].get('isError'), response
    assert calls[0]['max_steps'] == steps
    assert calls[0]['settle'] == settle
    assert calls[0]['act'] is True


@pytest.mark.parametrize('options', [{'max_steps':0}, {'max_steps':21}, {'settle':3000}, {'settle':float('nan')}])
def test_android_invalid_bounds_fail_before_device_access(monkeypatch, options):
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: pytest.fail('must validate before device access'))
    with pytest.raises(ValueError):
        mcp_server.tool_android_use({'goal':'open account menu', **options})


def test_busy_response_identifies_original_call_and_does_not_start_new_action(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    responses = []
    calls = []
    def handle(request):
        calls.append(request['id'])
        started.set()
        assert release.wait(3)
        return {'jsonrpc':'2.0', 'id':request['id'], 'result':{'content':[]}}
    def send(response):
        responses.append(response)
        if response['id'] == 1:
            completed.set()
    def incoming():
        yield json.dumps({'id':1,'method':'tools/call','params':{'name':'android_location'}})
        assert started.wait(3)
        yield json.dumps({'id':2,'method':'tools/call','params':{'name':'android_use'}})
        release.set()
        assert completed.wait(3)
    monkeypatch.setattr(mcp_server, 'handle', handle)
    monkeypatch.setattr(mcp_server, 'send', send)
    monkeypatch.setattr(mcp_server, 'load_env', lambda: None)
    monkeypatch.setattr(mcp_server.sys, 'stdin', incoming())
    monkeypatch.setattr(mcp_server.betterwright, 'close_sessions', lambda: None)
    assert mcp_server.main() == 0
    assert calls == [1]
    busy = next(response for response in responses if response['id'] == 2)['result']
    assert busy['isError']
    text = busy['content'][0]['text']
    assert 'Request not started' in text
    assert 'android_location (request ID 1)' in text
    assert any(response['id'] == 1 and not response['result'].get('isError') for response in responses)


def test_next_call_is_accepted_when_previous_response_is_being_published(monkeypatch):
    publishing = threading.Event()
    queued = threading.Event()
    completed = threading.Event()
    calls, responses = [], []
    events = []
    new_event = threading.Event

    def event_factory():
        event = new_event()
        events.append(event)
        return event

    def handle(request):
        calls.append(request['id'])
        return {'jsonrpc': '2.0', 'id': request['id'], 'result': {'content': []}}

    def send(response):
        assert not events[0].is_set(), 'completed request must release busy before publishing its response'
        responses.append(response)
        if response['id'] == 1:
            publishing.set()
            assert queued.wait(3)
        else:
            completed.set()

    def incoming():
        yield json.dumps({'id': 1, 'method': 'tools/call', 'params': {'name': 'android_read'}})
        assert publishing.wait(3)
        queued.set()
        yield json.dumps({'id': 2, 'method': 'tools/call', 'params': {'name': 'android_location'}})
        assert completed.wait(3)

    monkeypatch.setattr(mcp_server, 'handle', handle)
    monkeypatch.setattr(mcp_server, 'send', send)
    monkeypatch.setattr(mcp_server, 'load_env', lambda: None)
    monkeypatch.setattr(mcp_server.threading, 'Event', event_factory)
    monkeypatch.setattr(mcp_server.sys, 'stdin', incoming())
    monkeypatch.setattr(mcp_server.betterwright, 'close_sessions', lambda: None)
    assert mcp_server.main() == 0
    assert calls == [1, 2]
    assert all(not response['result'].get('isError') for response in responses)


def test_android_read_exposes_unlabelled_navigation_controls(monkeypatch):
    obs = android.Observation('fixture-device', android.parse_nodes('''
      <hierarchy><node class="android.view.View" clickable="true"
      resource-id="app:id/nav" bounds="[900,1920][1080,2060]" /></hierarchy>
    '''), (1080, 2160))
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='fixture-device'))
    monkeypatch.setattr(android, 'snapshot', lambda serial: obs)
    text = mcp_server.tool_android_read({})
    assert 'no visible text' in text
    assert obs.targets[0]['description'] in text
    assert 'Tappable controls' in text


def test_android_screenshot_is_returned_as_mcp_image(monkeypatch):
    obs = android.Observation('S', [], (1080, 2160))
    png = b'\x89PNG\r\n\x1a\nfixture'
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='S'))
    monkeypatch.setattr(android, 'snapshot', lambda serial: obs)
    monkeypatch.setattr(android, 'exec_out', lambda serial, args: png)
    response = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'android_read', 'arguments': {'include_screenshot': True}}})
    content = response['result']['content']
    assert [block['type'] for block in content] == ['text', 'image']
    assert content[1]['mimeType'] == 'image/png'
    import base64
    assert base64.b64decode(content[1]['data']) == png


@pytest.mark.parametrize('args', [{'action': 'unknown'}, {'action': 'location'}, {'action': 'location', 'account': ' '},
                                  {'action': 'accounts', 'timeout': float('nan')}, {'action': 'accounts', 'timeout': 1000}])
def test_facebook_invalid_arguments_fail_before_touching_device(monkeypatch, args):
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: pytest.fail('invalid arguments must not touch phone'))
    with pytest.raises(ValueError):
        mcp_server.tool_android_facebook(args)


def test_facebook_mcp_routes_to_deterministic_workflow_without_chooser(monkeypatch):
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='S'))
    monkeypatch.setattr(mcp_server, 'JevChooser', lambda: pytest.fail('Facebook workflow must not instantiate a chooser'))
    monkeypatch.setattr(mcp_server.facebook_android, 'run', lambda serial, **kwargs: {
        'device': serial, 'state': 'accounts', 'accounts': ['Name'], 'active_account': 'Name'})
    response = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'android_facebook', 'arguments': {'action': 'accounts'}}})
    result = json.loads(response['result']['content'][0]['text'])
    assert result['accounts'] == ['Name']


@pytest.mark.parametrize("args", [
    {}, {"account": " "}, {"account": "Shyam", "place": " "},
    {"account": "Shyam", "audience": "Friends"},
    {"account": "Shyam", "publish": "true"},
    {"account": "Shyam", "publish": True},
    {"account": "Shyam", "publish": True, "run_id": " "},
    {"account": "Shyam", "timeout": True},
    {"account": "Shyam", "timeout": 61},
    {"account": "Shyam", "serial": " "},
    {"account": "Shyam", "resume_token": "../bad"},
])
def test_facebook_flow_invalid_arguments_fail_before_device_or_state(monkeypatch, args):
    monkeypatch.setattr(mcp_server, 'android_device',
                        lambda args: pytest.fail('invalid args must fail before device discovery'))
    monkeypatch.setattr(mcp_server.facebook_flow_state, 'load',
                        lambda token: pytest.fail('invalid args must fail before checkpoint access'))
    with pytest.raises(ValueError):
        mcp_server.tool_android_facebook_flow(args)


def test_facebook_flow_handler_checks_resume_binding_before_device(monkeypatch):
    token = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setattr(mcp_server.facebook_flow_state, 'load', lambda token: {
        "serial": "S", "account": "Other", "place": "Manila, Philippines",
        "audience": "Public"})
    monkeypatch.setattr(mcp_server, 'android_device',
                        lambda args: pytest.fail('binding mismatch must fail before device discovery'))
    with pytest.raises(ValueError, match="different account"):
        mcp_server.tool_android_facebook_flow({"account": "Shyam", "resume_token": token})


def test_facebook_flow_handler_forwards_defaults_and_explicit_publish(monkeypatch):
    monkeypatch.setattr(mcp_server, 'android_device', lambda args: SimpleNamespace(serial='S'))
    calls = []
    monkeypatch.setattr(mcp_server.facebook_flow, 'run',
                        lambda serial, **kwargs: calls.append((serial, kwargs)) or {
                            "state": "pre-submit", "stage": "pre-submit"})
    result = json.loads(mcp_server.tool_android_facebook_flow({
        "account": "Shyam Desai", "publish": True, "run_id": "job-123"}))
    assert result["stage"] == "pre-submit"
    assert calls == [("S", {"account": "Shyam Desai", "place": "Manila, Philippines",
                             "audience": "Public", "resume_token": None,
                             "timeout": 55.0, "publish": True,
                             "run_id": "job-123"})]


def test_facebook_flow_tool_is_registered_with_public_audience_schema():
    assert "android_facebook_flow" in mcp_server.HANDLERS
    schema = next(tool["inputSchema"] for tool in mcp_server.TOOLS
                  if tool["name"] == "android_facebook_flow")
    assert schema["properties"]["audience"]["enum"] == ["Public"]
    assert schema["properties"]["publish"]["default"] is False
    assert schema["required"] == ["account"]


def test_mobile_prompt_prefers_verified_workflow_and_clear_blockers():
    prompt = mcp_server.MOBILE_PROMPT_TEMPLATE
    assert 'android_facebook' in prompt
    assert 'identity_verified=true' in prompt
    assert 'include_screenshot=true' in prompt
    assert 'never repeat an uncertain account switch' in prompt
