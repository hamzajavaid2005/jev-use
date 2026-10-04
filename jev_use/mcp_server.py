"""jev-use — browser and Android use with Jev making the decisions, as an MCP server.

Any harness (Command Code, Claude Code, Cursor, Codex) can call this. The harness
brings the reasoning; this server brings the device and Jev brings the decisions.

Deliberately small — a large tool schema is paid for in the model's context on
every turn:

    browser_profiles   which Chromium-family browsers are open, and whether CDP works
    browser_open       launch a profile from a private copy, with a CDP endpoint
    browser_use        drive the page toward a goal; Jev picks each action
    browser_script     run a prepared BetterWright Playwright workflow in one call
    browser_extract    ask typed questions about the page, get typed answers
    browser_read       return the page's text

    android_devices    which phones are attached over adb
    android_use        drive the phone toward a goal; Jev picks each action
    android_read       return the screen's text
    android_location   the location Facebook attributes to the signed-in account
    android_facebook   deterministic saved-account location workflow
    android_facebook_flow   checkpointed Facebook check-in and logout workflow

Android needs no CDP equivalent (adb is the channel, and it is always there),
and it needs no `extract`: the view
hierarchy yields a kilobyte or two of text, where a web dashboard yields twenty,
so handing the harness the text costs almost nothing. The Facebook workflow
keeps repeated account navigation and identity checks out of the model loop;
Facebook's own page is the authority on its inferred primary location.

The desktop / accessibility surface was removed. On GNOME/Wayland it cannot
attach to an existing browser profile, and that dead end is what made agents
burn turns. Android does not go through it either — see `android.py` for why
scrcpy's window is the wrong thing to point a window-based driver at.

Wire it up:

    cmd mcp add --scope user jev-use -- /path/to/.venv/bin/python -m jev_use.mcp_server

Speaks MCP over stdio. Nothing but JSON-RPC goes to stdout.
"""

from __future__ import annotations

import atexit
import base64
import json
import math
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from .browser import DEFAULT_PROFILE, attach, read as browser_read, run as browser_run
from .browser import running_profiles, cdp_alive, _pick_profile
from .browser import navigate_and_settle, read_many as browser_read_many
from .choosers import JevChooser
from .cache import PlanCache
from .driver import Driver, DriverError
from .extract import ExtractError, ask as extract_ask
from pathlib import Path
from .profiles import find_profile, profile_registry as local_profiles, start_profile
from .text_model import TextModel

from . import android as android_engine
from . import facebook_android
from . import facebook_audit
from . import facebook_flow
from . import facebook_flow_state
from . import gologin
from . import harness
from . import betterwright

SERVER_NAME = "jev-use"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", PROTOCOL_VERSION, "2025-11-25"}
CACHE_PATH = Path(__file__).resolve().parent.parent / ".jev-browser-cache.json"

# Single source of truth: the tool schema and the handler must agree. They drifted
# once (schema said one thing, handler another) which silently changed behaviour.
DEFAULT_MAX_STEPS = 8
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_SETTLE = 3.0
#: Tabs browser_read_many reads at once. Deliberately small: each worker is its own
#: driver process and its own browser tab, Chrome throttles background tabs, and the
#: returns flatten fast past a handful.
DEFAULT_READ_CONCURRENCY = 3
# Android settles for less time, for a measured reason — see android.DEFAULT_SETTLE.
ANDROID_DEFAULT_SETTLE = android_engine.DEFAULT_SETTLE
#: The location page is a webview with no ready signal, so it is polled. Single
#: sourced here so the tool schema and the handler cannot drift apart.
ANDROID_LOCATION_TIMEOUT = android_engine.LOCATION_LOAD_TIMEOUT


# -- helpers ----------------------------------------------------------------


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for JSON-RPC."""
    print(message, file=sys.stderr, flush=True)


def load_env() -> None:
    """.env next to the package, so the API key does not have to be duplicated."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def attach_helpfully(
    driver: Driver, profile: str | None = None, port: int | None = None
) -> Any:
    """Attach to the requested profile (default: the real one), or explain the fix.

    An explicit `port` wins over profile matching. It is the one route that reaches
    a browser discovery cannot see, and the only route that reaches a GoLogin
    profile: Orbita picks its debug port at launch, so there is nothing to guess.
    """
    return attach(driver, port=port, profile=profile)


def _port(args: dict[str, Any]) -> int | None:
    """The optional `port` argument, as an int or None. Never raises on junk."""
    value = args.get("port")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# -- driver sessions --------------------------------------------------------
#
# Each browser_* call used to spawn its own `cua-driver mcp`, then re-run
# discovery (`ps`, up to two `/json/version` probes) and a `list_windows` bind —
# and tear all of it down again. A harness that follows browser_use with
# browser_read and browser_extract pays that three times for one page.
#
# This keeps one transport + Target alive and reuses it while it is healthy, and
# resets it only when the child process is gone, the requested profile changed, or
# a call fails. Two transports can sit here: a `Driver` (the `page` tool, whose
# process is ours alone, so the unrestricted permission mode it runs under stays
# contained) or a `Harness` (browser-harness's CDP client, one websocket, no
# process at all). `browser._js` routes to whichever it was handed.

_SESSION_LOCK = threading.Lock()
_SESSION: dict[str, Any] = {"driver": None, "target": None, "profile": None}

#: The GoLogin profile this process started, if any. Held so `browser_close` can
#: stop it: the SDK commits cookies and login state back to GoLogin on stop, so a
#: profile that is never stopped loses everything the session did.
_GOLOGIN: dict[str, Any] = {"session": None}


def _close_session_locked() -> None:
    """Close the cached driver. Caller must hold `_SESSION_LOCK`."""
    driver = _SESSION["driver"]
    _SESSION.update(driver=None, target=None, profile=None)
    if driver is not None:
        try:
            driver.close()
        except Exception:
            pass


def reset_browser_session() -> None:
    """Drop the cached session so the next call starts a fresh driver."""
    with _SESSION_LOCK:
        _close_session_locked()


def _harness_wanted(port: int | None) -> bool:
    """Whether this endpoint should be driven over the browser-harness transport.

    `JEV_USE_TRANSPORT` forces the choice (`driver` / `harness`); the default is
    `auto`, which uses a persistent CDP connection for any explicit browser port
    when the client is installed. Profile requests resolve their port once.
    """
    mode = (os.environ.get("JEV_USE_TRANSPORT") or "auto").strip().lower()
    if mode == "driver":
        return False
    if mode == "harness":
        if not harness.available():
            raise harness.HarnessError(harness.INSTALL_HINT)
        return port is not None
    if port is None or not harness.available():
        return False
    return True


def _start_session(profile: str | None, port: int | None) -> tuple[Any, Any]:
    """A started transport and its Target — the harness where it applies, else a driver."""
    if port is None and harness.available() and (os.environ.get("JEV_USE_TRANSPORT") or "auto") != "driver":
        from .browser import _pick_profile
        chosen, _ = _pick_profile(running_profiles(), profile)
        if chosen is not None:
            port = chosen.port
    if _harness_wanted(port):
        session = harness.Harness(port=port)  # type: ignore[arg-type]
        session.start()
        try:
            return session, attach(session, port=port, profile=profile)
        except Exception:
            session.close()
            raise
    driver = Driver()
    driver.start()
    try:
        return driver, attach_helpfully(driver, profile, port)
    except Exception:
        driver.close()
        raise


def _browser_session(
    profile: str | None, port: int | None = None
) -> tuple[Any, Any]:
    """A live `(transport, target)` for `profile`/`port`, reusing the cached one when valid."""
    betterwright.close_sessions()
    key = f"{profile or ''}|{port if port is not None else ''}"
    with _SESSION_LOCK:
        driver = _SESSION["driver"]
        if (
            driver is not None
            and getattr(driver, "alive", False)
            and _SESSION["profile"] == key
            and _SESSION["target"] is not None
        ):
            return driver, _SESSION["target"]

        _close_session_locked()
        session, target = _start_session(profile, port)
        _SESSION.update(driver=session, target=target, profile=key)
        return session, target


def with_browser_session(
    profile: str | None, body: Any, port: int | None = None
) -> tuple[Any, Any]:
    """Run `body(driver, target)` on a reused session.

    On a DriverError the session is dropped so the next call is fresh, but the
    error is re-raised rather than retried: re-running the body could repeat a
    partial action.
    """
    driver, target = _browser_session(profile, port)
    try:
        return body(driver, target), target
    except DriverError:
        reset_browser_session()
        raise


atexit.register(reset_browser_session)


def stop_gologin_session() -> None:
    """Stop the GoLogin profile we started, if any, so its work is committed.

    Registered at exit as well as exposed as `browser_close`. Leaving a GoLogin
    profile running is not harmless the way leaving Chrome open is: the SDK only
    writes cookies and login state back to GoLogin when it stops, so a server that
    exits without stopping it drops the session on the floor.
    """
    betterwright.close_sessions()
    reset_browser_session()
    session = _GOLOGIN.get("session")
    if session is None:
        return
    _GOLOGIN["session"] = None
    try:
        session.stop()
    except Exception:
        pass


atexit.register(stop_gologin_session)


def render(result: Any) -> str:
    lines = []
    if getattr(result, "subgoals", None):
        lines.append("plan: " + " -> ".join(result.subgoals))
    for step in result.steps:
        lines.append(("> " if step.executed else "  ") + step.note)
    per = result.seconds / result.actions if result.actions else 0.0
    lines.append(
        f"url={result.url}\noutcome={result.outcome} actions={result.actions} "
        f"snapshots={result.snapshots} seconds={result.seconds:.2f} "
        f"per_action_ms={per * 1000:.0f}"
    )
    return "\n".join(lines)


# -- prompts ----------------------------------------------------------------

PROMPT_NAME = "browser-use"

PROMPT_TEMPLATE = """Task: {task}
Call browser_profiles once; reuse the matching port or browser_open the requested profile.
For a known URL use browser_read(port=N, url=...) directly. For several URLs use
browser_read_many. For one exact click/fill/scroll use browser_action. For any
repeated account, login, form, wizard, or checkout workflow, MUST use one
browser_script call per account with a prepared BetterWright Playwright snippet;
do not spend a model turn on every click and do not switch to agent-browser.
Use browser_use with Jev only when the controls are genuinely unknown or the
prepared script failed and needs one short recovery step. Verify the script result.
Never substitute a different browser for a requested account. GoLogin needs
vendor=\"gologin\" and browser_close afterward.
When the task says all GoLogin profiles and supplies an ordered password list,
do not ask profile-scope or account-distribution questions: enumerate every
returned exact profile ID, sort profiles numerically by their Profile number,
explicitly open Profile 1 first (never API/newest-first), then map passwords to saved-account cards by position,
skip accounts beyond the password list or at MFA, close the current profile,
and continue to the next profile.
If tools are missing, search once then use `jev-use call <tool>` in the shell.
Do not build clients or debug the installation during the task. Retry once at
most; report failures and stop. Quote only what you read.
"""

MOBILE_PROMPT_NAME = "mobile-use"

MOBILE_PROMPT_TEMPLATE = """Task: {task}
Call android_devices once; run phone calls sequentially and reuse its serial.
For a full Facebook location audit, use android_facebook(action="audit") in
bounded chunks. Save and pass its resume_token until complete=true. The audit
enumerates observed names and processes them sequentially, persisting each
finished outcome. On blocked, inspect the phone before choosing retry_current=true
(checks the active account without selecting it) or continue_after_blocker=true
(skips it). These options are exclusive; uncertain accounts are never selected
twice. For an explicit preselection session_blocker, use unattempted_accounts:
those names have not been processed. If uncertain_account is present, selection
may have happened; do not call it unattempted. Report only returned results and
never say the full list was scanned.
Remembered account cards do not prove valid sessions. Missing cards on a
logged-out saved-account screen do not prove those accounts are logged out.
On the saved-account landing screen, audit/accounts can select one observed
saved card once, verify the resulting session, then enumerate the full picker.
Location can select the exact requested visible saved card. The account-check
request authorizes those taps; do not ask the user to tap a card merely because
Facebook starts on this screen. Ask for help if sign-in requests credentials
or verification. Cards alone never prove live sessions.
For session_expired/pending_login, resolve the session blocker
before continuation; readiness checks preserve the checkpoint while blocked.
A partial final report retains failed account outcomes.
Quote location only when state=location and identity_verified=true.
For one account use android_facebook(action="location", account="EXACT_RETURNED_NAME").
Both paths handle account selection and identity verification without model-guessed taps.
For a requested Facebook check-in followed by logout, use one
android_facebook_flow call with the exact account and place. Set publish=true
only when publishing is authorized; include a stable run_id for the first call
and reuse it if that request times out before returning. The flow verifies the
account, reports its primary location, prepares and verifies a Public post
preview, then publishes once and verifies logout. Resume returned checkpoints
with resume_token.
Uncertain submitting/logging_out checkpoints require inspection and are never
replayed automatically. The flow uses no decision model.
For locked/network_error/login/authentication_required, report the required user
action. For timeout/unsupported_ui/identity_mismatch, inspect once with
android_read(include_screenshot=true); never repeat an uncertain account switch.
Primary location is inferred; these tools do not write it. A profile-city edit
or location-tagged post is a separate action; clarify ambiguous "post location"
requests instead of assuming fraud or inventing a write operation.
For other tasks use android_read and android_use(act=true). Unique exact control
descriptions support deterministic taps with max_steps=1, decompose=false,
use_cache=false. Inspect screenshot for unlabelled controls; do not guess meanings.
Pass serial when several devices are connected.
Read again to verify. If tools are missing use `jev-use call <tool>` in the shell.
No helper clients or repair loops. Retry a transient failure once; otherwise
report it and stop. The phone must be unlocked with USB debugging accepted.
"""

PROMPT_TEMPLATES: dict[str, str] = {
    PROMPT_NAME: PROMPT_TEMPLATE,
    MOBILE_PROMPT_NAME: MOBILE_PROMPT_TEMPLATE,
}

PROMPTS: list[dict[str, Any]] = [
    {
        "name": PROMPT_NAME,
        "description": (
            "Do a task in the user's own browser (usage pages, dashboards, repos, "
            "deployments) and report what the pages say."
        ),
        "arguments": [
            {
                "name": "task",
                "description": "The task in plain English.",
                "required": True,
            }
        ],
    },
    {
        "name": MOBILE_PROMPT_NAME,
        "description": (
            "Do a task on the user's own Android phone (apps, settings, messages) and "
            "report what the screens say."
        ),
        "arguments": [
            {
                "name": "task",
                "description": "The task in plain English.",
                "required": True,
            }
        ],
    },
]


def prompt_messages(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    template = PROMPT_TEMPLATES.get(name)
    if template is None:
        return None
    task = (arguments or {}).get("task", "").strip() or "(no task given)"
    kind = "Phone" if name == MOBILE_PROMPT_NAME else "Browser"
    return {
        "description": f"{kind} task: {task[:80]}",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": template.format(task=task)}}
        ],
    }


# -- tools ------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "browser_profiles",
        "description": "List running browsers and saved profiles. Call FIRST, once; reuse a matching live CDP port.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "only_running": {"type": "boolean", "default": False},
                "available": {"type": "boolean", "default": False},
                "filter": {
                    "type": "string",
                    "description": "Only profiles whose name contains this substring.",
                },
            },
        },
    },
    {
        "name": "browser_open",
        "description": "Open a Chrome profile SNAPSHOT (refresh to update): CDP requires a non-default data directory. For native GoLogin use vendor=gologin. Returns the port to use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Display name (or id) of the profile to open.",
                },
                "vendor": {
                    "type": "string",
                    "enum": ["auto", "chrome", "gologin"],
                    "default": "auto",
                    "description": "Chrome snapshot or native GoLogin; auto tries Chrome first.",
                },
                "port": {
                    "type": "integer",
                    "description": "CDP port; use the port returned by browser_open.",
                },
                "url": {"type": "string", "description": "Page to open. Default about:blank."},
                "headless": {
                    "type": "boolean",
                    "default": False,
                    "description": "Hide the browser ONLY when the user explicitly requests headless. Default false keeps Chrome and GoLogin visible; CDP actions do not use OS input.",
                },
                "background": {"type": "boolean", "default": False, "description": "Legacy option: does not hide the browser. Background CDP automation stays visible and does not use the system mouse or keyboard. Use headless only if explicitly requested."},
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Chrome only: re-copy the profile, picking up logins made since the last copy.",
                },
            },
            "required": ["profile"],
        },
    },
    {
        "name": "browser_close",
        "description": "Stop the GoLogin profile this server opened and save its cookies.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_use",
        "description": "Perform browser interactions toward a short goal. Set act=true to act. For reading known URLs use browser_read(url=...) instead. Read after actions to verify.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to accomplish in the page."},
                "url": {
                    "type": "string",
                    "description": "Optional URL to load before starting.",
                },
                "act": {
                    "type": "boolean",
                    "default": False,
                    "description": "Actually click. Default false: decide and validate only.",
                },
                "max_steps": {"type": "integer", "default": DEFAULT_MAX_STEPS, "minimum": 1, "maximum": 20},
                "min_confidence": {
                    "type": "number",
                    "default": DEFAULT_MIN_CONFIDENCE,
                    "description": "Stop and report rather than act below this confidence.",
                },
                "settle": {
                    "type": "number",
                    "default": DEFAULT_SETTLE,
                    "minimum": 0,
                    "maximum": 10,
                    "description": "Wait bound in SECONDS (0–10), never milliseconds. Use 5, not 5000.",
                },
                "port": {
                    "type": "integer",
                    "description": "CDP port; use the port returned by browser_open.",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name; explicit port wins.",
                },
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": "Reuse a successful plan; stale plans are re-decided.",
                },
                "decompose": {
                    "type": "boolean",
                    "default": True,
                    "description": "Plan multi-step goals when a text model is configured.",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "browser_action",
        "description": "One exact action, then return page text. No Jev call. Click/fill require a unique visible label; for ambiguous controls use browser_use.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["click", "fill", "scroll_down", "scroll_up", "back"]},
                "label": {"type": "string", "description": "Exact visible control label for click/fill."},
                "text": {"type": "string", "description": "Literal value for fill."},
                "port": {"type": "integer"},
                "profile": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_script",
        "description": (
            "Run one prepared BetterWright Playwright script against the existing "
            "browser. Use this fast deterministic path for known forms and workflows; "
            "it attaches to the GoLogin CDP browser and does not launch another browser."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Playwright JavaScript executed by BetterWright. Use page, "
                        "snapshot, human, and screenshot; return a short JSON-safe result. "
                        "Use submission={run_id,account} to click Create Page once before this code; confirmCreated and logout can then run together. "
                        "Repeated account workflows also have a checkpointed workflow helper: "
                        "begin, browseFeed, beforeCreate, confirmCreated, and loggedOut."
                    ),
                },
                "port": {
                    "type": "integer",
                    "description": "CDP port returned by browser_open.",
                },
                "profile": {
                    "type": "string",
                    "description": "Running profile when an explicit port is unavailable.",
                },
                "timeout": {
                    "type": "number",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 900,
                    "description": "Hard deadline in seconds, from 1 to 900; timed-out scripts are never replayed.",
                },
                "page_url": {
                    "type": "string",
                    "description": "Exact existing tab URL to select when multiple tabs are open. Omit on subsequent script calls to keep the selected tab.",
                },
                "submission": {
                    "type": "object",
                    "description": "Explicit Create Page attempt. Checks actionability without clicking first, preserves reservations on selector failure, then journals and clicks once. Code runs afterward to confirm and logout. Never include another creation click in code.",
                    "properties": {
                        "run_id": {"type": "string"},
                        "account": {"type": "string"},
                        "selector": {"type": "string", "description": "Optional observed selector; default is role=button named Create Page, including div role=button."},
                    },
                    "required": ["run_id", "account"],
                    "additionalProperties": False,
                },
                "checkpoint_scope": {
                    "type": "string",
                    "description": "Stable non-secret profile identity for workflow checkpoints across browser restarts. Automatically uses the GoLogin ID when this server opened it.",
                },
                "dismiss_overlays": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Dismiss recognized cookie/promotional overlays and the "
                        "Facebook 'Sign in as' chooser before the script. Password "
                        "and unrelated dialogs are untouched."
                    ),
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Extract typed answers from page text using Jev. For ordinary questions use browser_read.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "object",
                    "description": (
                        "name -> {type: choice|score|noul, instructions, criteria}. "
                        "choice needs criteria as an object, score as a list of 2-10 "
                        "ordered levels, noul needs neither."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Ask about this text instead of reading the page.",
                },
                "profile": {"type": "string", "description": "Which browser profile."},
                "port": {"type": "integer"},
            },
            "required": ["questions"],
        },
    },
    {
        "name": "browser_read",
        "description": "Navigate to an optional URL and return visible page text. No decision model needed. Pass port from browser_open.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Navigate here and read in one call; no decision model needed."},
                "max_chars": {"type": "integer", "default": 6000, "description": "Output limit, up to 20000."},
                "port": {"type": "integer", "description": "Explicit CDP port."},
                "profile": {
                    "type": "string",
                    "description": "Which browser profile to read, as in browser_use.",
                },
            },
        },
    },
    {
        "name": "browser_read_many",
        "description": "Read known URLs in parallel tabs. Pass the browser port. Prefer one call over a loop of reads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The pages to read, in the order you want them back.",
                },
                "concurrency": {
                    "type": "integer",
                    "default": DEFAULT_READ_CONCURRENCY,
                    "description": "How many tabs to read at once (1-8).",
                },
                "max_chars": {"type": "integer", "default": 6000, "description": "Text limit per page, up to 20000."},
                "profile": {
                    "type": "string",
                    "description": "Which browser profile to read, as in browser_use.",
                },
                "port": {
                    "type": "integer",
                    "description": "CDP port; use the port returned by browser_open.",
                },
            },
            "required": ["urls"],
        },
    },
    {
        "name": "android_devices",
        "description": "List connected Android devices. Call once before phone tasks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "android_use",
        "description": "Perform phone actions toward a short goal. Set act=true to act. Pass serial for multiple devices; android_read afterward verifies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What to accomplish on the phone."},
                "serial": {
                    "type": "string",
                    "description": "Device serial; required for multiple devices.",
                },
                "act": {
                    "type": "boolean",
                    "default": False,
                    "description": "Actually tap. Default false: decide and validate only.",
                },
                "max_steps": {"type": "integer", "default": DEFAULT_MAX_STEPS, "minimum": 1, "maximum": 20},
                "min_confidence": {
                    "type": "number",
                    "default": DEFAULT_MIN_CONFIDENCE,
                    "description": "Stop and report rather than act below this confidence.",
                },
                "settle": {
                    "type": "number",
                    "default": ANDROID_DEFAULT_SETTLE,
                    "minimum": 0,
                    "maximum": 10,
                    "description": "Upper bound in seconds to wait for the screen to move.",
                },
                "use_cache": {
                    "type": "boolean",
                    "default": True,
                    "description": "Reuse a successful plan; stale plans are re-decided.",
                },
                "decompose": {
                    "type": "boolean",
                    "default": True,
                    "description": "Plan multi-step goals when a text model is configured.",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "android_read",
        "description": "Read phone text and exact tappable controls. Set include_screenshot=true to inspect unlabelled icons; do not guess their meaning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Which device, from android_devices."},
                "include_screenshot": {"type": "boolean", "default": False, "description": "Also return the actual phone screen as an image, for unknown UI."},
            },
        },
    },
    {
        "name": "android_facebook",
        "description": "Deterministic Facebook account workflow; no decision model needed. Prefer action=audit in timeout-safe chunks and pass its resume_token until complete. From the saved-account sign-in screen it can try one observed saved card and verify the active identity before enumerating. It enumerates exact observed names, persists each verified result and processes accounts sequentially. Use accounts/location for a single manual read. If Facebook is frozen on login or a blank page, use restart once with the exact requested account; it force-stops/relaunches while preserving app data, then verifies identity before reading location. Reads primary location; does not edit it or publish posts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Device from android_devices."},
                "action": {"type": "string", "enum": ["accounts", "location", "restart", "audit"]},
                "account": {"type": "string", "description": "Exact observed account name; required for action=location or restart."},
                "timeout": {"type": "number", "default": facebook_audit.DEFAULT_ACCOUNT_TIMEOUT, "minimum": 1, "maximum": 60,
                            "description": "Maximum total seconds for each account selection and location read."},
                "chunk_size": {"type": "integer", "default": facebook_audit.DEFAULT_CHUNK_SIZE, "minimum": 1, "maximum": facebook_audit.MAX_CHUNK_SIZE,
                               "description": "Maximum sequential accounts to process in this timeout-safe call."},
                "chunk_budget_seconds": {"type": "number", "default": facebook_audit.CHUNK_BUDGET_SECONDS, "minimum": 10, "maximum": 55,
                                         "description": "Maximum wall time for this audit call, including enumeration."},
                "resume_token": {"type": "string", "description": "Opaque token returned by an earlier incomplete audit."},
                "continue_after_blocker": {"type": "boolean", "default": False,
                                            "description": "After inspecting and resolving the phone state, skip any uncertain in-flight account and continue with later accounts."},
                "retry_current": {"type": "boolean", "default": False,
                                  "description": "After inspecting the phone, recheck the already-active account without selecting it again. Requires resume_token and a blocked audit."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "android_facebook_flow",
        "description": "Run a deterministic, checkpointed Facebook flow for one exact account: verify identity, read primary location, prepare a Public check-in at the requested place, publish once only when publish=true, and verify logout. No decision model is used. Resume with the returned token; uncertain Publish or logout stages are never replayed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Device serial from android_devices."},
                "account": {"type": "string", "description": "Exact Facebook account name to use."},
                "place": {"type": "string", "default": "Manila, Philippines", "description": "Exact check-in place."},
                "audience": {"type": "string", "enum": ["Public"], "default": "Public", "description": "Currently supported audience."},
                "publish": {"type": "boolean", "default": False, "description": "Explicitly authorize the verified Public check-in to be published."},
                "run_id": {"type": "string", "description": "Stable caller-generated identifier for this publishing task; reuse it if the first response is lost."},
                "resume_token": {"type": "string", "description": "Opaque checkpoint token returned by an earlier call."},
                "timeout": {"type": "number", "default": 55, "minimum": 1, "maximum": 60, "description": "Maximum workflow duration in seconds."},
            },
            "required": ["account"],
        },
    },
    {
        "name": "android_location",
        "description": "Read the Facebook account location. Quote location only when state=location.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "serial": {"type": "string", "description": "Which device, from android_devices."},
                "timeout": {
                    "type": "number",
                    "default": ANDROID_LOCATION_TIMEOUT,
                    "description": "Upper bound in seconds to wait for the page to name a location.",
                },
            },
        },
    },
]


def _gologin_lines(filter_text: str) -> list[str]:
    """The GoLogin section of browser_profiles, or an empty list.

    A GoLogin profile is listed here rather than under AVAILABLE because it is not
    a Chrome profile: it opens in its own browser, not through browser_open's copy.
    Listing needs the API token, since GoLogin profiles live on the account; without
    one we only say that GoLogin is present and how to enable it.
    """
    if not gologin.token():
        from . import host

        root = host.gologin_browser_root()
        if root.exists():
            return [
                "",
                f"GOLOGIN — GoLogin is installed ({root}) but no API token is set.",
                "  Add one to open its profiles in their own browser:",
                "    jev-use install --gologin-token=<token>",
            ]
        return []

    try:
        found = gologin.profiles()
    except gologin.GoLoginError as exc:
        return ["", f"GOLOGIN — {exc}"]

    if filter_text:
        found = [p for p in found if filter_text in p.name.lower()]
    found = sorted(found, key=gologin.profile_sort_key)
    lines = [
        "",
        f"GOLOGIN ({len(found)} profiles; each runs in its own browser, not Chrome)",
    ]
    if found:
        lines.append(
            "  BATCH ORDER: process numeric Profile 1 first, then Profile 2, Profile 3, and so on"
        )
    if not found:
        lines.append("  (none match)")
    for profile in found:
        lines.append(f"  {profile.describe()}")
    lines.append('')
    lines.append('Open one with browser_open(profile="<name>", vendor="gologin").')
    return lines


def tool_browser_profiles(args: dict[str, Any]) -> str:
    """Running browsers and on-disk profiles, one line each."""
    filter_text = (args.get("filter") or "").lower()

    # Running browsers first: these are what browser_use can attach to right now.
    running: dict[str, Any] = {}
    for profile in running_profiles():
        existing = running.get(profile.profile_dir)
        # Keep the process that actually serves CDP when several share a profile.
        if existing is None or (profile.cdp and not existing.cdp):
            running[profile.profile_dir] = profile

    if not args.get("available"):
        lines = ["RUNNING"] if running else []
        usable = 0
        from .profiles import WORK_ROOT

        disk_profiles = local_profiles()
        by_slug = {p.workdir.name: p for p in disk_profiles}
        for profile in running.values():
            if profile.port is None:
                suffix = ""
            elif cdp_alive(profile.port):
                suffix, usable = "", usable + 1
            else:
                suffix = "  (port set but not answering)"
            # A prepared copy reports its slug; show the profile's real name.
            label = profile.describe()
            if str(WORK_ROOT) in profile.profile_dir:
                source = by_slug.get(Path(profile.profile_dir).name)
                if source:
                    label = f"{source.name!r} ({source.directory}) open from a copy cdp={'yes:' + str(profile.port) if profile.port else 'no'}"
            lines.append("  " + label + suffix)
        if not usable:
            if any(p.vendor == "gologin" for p in running.values()):
                lines.append(
                    "  (none drivable — a GoLogin/Orbita profile exposes CDP only when it "
                    "is started with a debugging port; start it through the GoLogin app/SDK "
                    "and pass the port it returns to browser_use)"
                )
            else:
                lines.append(
                    "  (none drivable — a running Chrome cannot expose CDP on its default "
                    "profile; use browser_open to launch a profile from its own copy)"
                )
        if args.get("only_running"):
            return "\n".join(lines)

    # On-disk profiles: what browser_open could start.
    lines = lines if not args.get("available") else []
    if not args.get("only_running"):
        try:
            profiles = disk_profiles if not args.get("available") else local_profiles()
        except FileNotFoundError as exc:
            # No Chrome at all is not the end of the report: a GoLogin user still
            # wants to see their profiles, so the section below is reached either way.
            profiles = []
            lines.append(f"\n{exc}")

        if filter_text:
            profiles = [
                p for p in profiles if filter_text in p.name.lower() or filter_text in p.directory.lower()
            ]
        lines.append("")
        lines.append(f"AVAILABLE ({len(profiles)} Chrome profiles on disk)")
        if not profiles:
            lines.append("  (none match)")
        for profile in profiles:
            marker = "  "
            if profile.port and cdp_alive(profile.port):
                marker = "> "  # already open and drivable
            lines.append(f"{marker}{profile.describe()}")
        lines.append('')
        lines.append('Open one with browser_open(profile="<name>").')

        lines += _gologin_lines(filter_text)

    return "\n".join(lines)


def _open_gologin(wanted: str, args: dict[str, Any]) -> str:
    """Launch a GoLogin profile in its own browser and report the CDP port."""
    try:
        match = gologin.find(wanted)
    except gologin.GoLoginError as exc:
        return str(exc)

    existing = _GOLOGIN.get("session")
    if existing is not None and existing.profile.id == match.id and cdp_alive(existing.port):
        return f"GoLogin profile {match.name!r} is already open and drivable on port {existing.port}."

    url = str(args.get("url") or "").strip()
    try:
        session = gologin.launch(
            match,
            port=_port(args),
            url=None if url in ("", "about:blank") else url,
            headless=args.get("headless") is True,
        )
    except gologin.GoLoginError as exc:
        return str(exc)

    _GOLOGIN["session"] = session
    transport = (
        "browser-harness CDP transport (one websocket, no per-call driver spawn)"
        if harness.available()
        else "the cua-driver `page` tool; install the harness for the faster path:\n"
        "    pip install 'jev-use[harness]'"
    )
    return (
        f"opened GoLogin profile {match.name!r} ({match.id}) in its own browser on "
        f"port {session.port}\n"
        "  this is the profile's real identity — fingerprint, proxy and cookies "
        "intact, not a copied Chrome profile.\n"
        f"  driving over: {transport}\n"
        f"  browser_use(port={session.port}) or browser_use(profile={match.name!r}) "
        "can attach now.\n"
        "  Call browser_close when the task is done, to stop it and save the profile."
    )


def tool_browser_open(args: dict[str, Any]) -> str:
    wanted = args["profile"]
    vendor = str(args.get("vendor") or "auto").lower()
    port = int(args.get("port", 9222))

    # Chrome first for "auto": a name that matches both must keep selecting Chrome,
    # which is what it did before GoLogin existed.
    match = None
    problem = ""
    if vendor in ("auto", "chrome"):
        try:
            match = find_profile(wanted)
        except (ValueError, FileNotFoundError) as exc:
            problem = str(exc)
            if vendor == "chrome":
                return problem

    if match is None:
        if vendor == "gologin" or (vendor == "auto" and gologin.token()):
            return _open_gologin(wanted, args)
        return problem or f"no profile matches {wanted!r}"

    if match.port and cdp_alive(match.port):
        return f"{match.name!r} is already open and drivable on port {match.port}."

    try:
        profile, workdir = start_profile(
            match.directory,
            port=port,
            refresh=bool(args.get("refresh", False)),
            url=str(args.get("url") or "about:blank"),
            background=args.get("headless") is True,
        )
    except (TimeoutError, FileNotFoundError, RuntimeError) as exc:
        return str(exc)

    return (
        f"opened {profile.name!r} ({profile.directory}) on port {port}\n"
        f"  copy: {workdir}\n"
        f"  logins come from the copy, taken at copy time.\n"
        f"  browser_use(profile={profile.directory!r}) can attach now."
    )


def tool_browser_close(args: dict[str, Any]) -> str:
    """Stop the GoLogin profile this server started, committing its state."""
    betterwright.close_sessions()
    reset_browser_session()
    session = _GOLOGIN.get("session")
    if session is None:
        return "no GoLogin profile was started by this server, so there is nothing to close."
    _GOLOGIN["session"] = None
    try:
        session.stop()
    except Exception as exc:  # noqa: BLE001 - report whatever the SDK raises
        return f"could not stop the GoLogin profile {session.profile.name!r}: {exc}"
    return (
        f"stopped GoLogin profile {session.profile.name!r}; its cookies and login "
        "state are saved back to GoLogin."
    )


def _bounded_number(args: dict, name: str, default: float, low: float, high: float) -> float:
    value = float(args.get(name, default))
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}; settle is in seconds, not milliseconds")
    return value


def tool_browser_use(args: dict[str, Any]) -> str:
    settle = _bounded_number(args, "settle", DEFAULT_SETTLE, 0, 10)
    max_steps = int(_bounded_number(args, "max_steps", DEFAULT_MAX_STEPS, 1, 20))
    goal = args["goal"]
    act = bool(args.get("act", False))

    def body(driver: Driver, target: Any) -> Any:
        if args.get("url"):
            _background_tab(driver, target)
            # Points the page at the URL and waits for it to actually arrive. The
            # baseline is read from the live page, not the target's cached URL.
            navigate_and_settle(
                driver, target, args["url"], settle
            )

        return browser_run(
            driver,
            target,
            goal,
            JevChooser(),
            act=act,
            max_steps=max_steps,
            min_confidence=float(args.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
            settle=settle,
            writer=TextModel(),
            decompose=bool(args.get("decompose", True)),
            cache=PlanCache(CACHE_PATH) if args.get("use_cache", True) else None,
        )

    result, target = with_browser_session(args.get("profile"), body, _port(args))
    return f"port={target.port} pid={target.pid}\n" + render(result)


def tool_browser_action(args: dict[str, Any]) -> str:
    from .actions import perform
    output, _ = with_browser_session(
        args.get("profile"),
        lambda session, page: perform(session, page, args["action"], args.get("label", ""), args.get("text")),
        _port(args),
    )
    return output


def tool_browser_extract(args: dict[str, Any]) -> str:
    questions = args.get("questions") or {}
    text = args.get("text")

    if text is None:
        try:
            text, _ = with_browser_session(
                args.get("profile"),
                lambda driver, target: browser_read(driver, target),
                _port(args),
            )
        except DriverError as exc:
            return str(exc)

    try:
        result = extract_ask(text or "", questions)
    except ExtractError as exc:
        return f"extract failed: {exc}"

    lines = [
        f"model={result['model']} chars={result['chars_asked']}",
        "",
    ]
    for name, answer in result["answers"].items():
        if "error" in answer:
            lines.append(f"{name}: {answer['error']}")
            continue
        value = answer.get("value")
        conf = answer.get("confidence")
        tail = f"  (confidence {conf})" if conf is not None else ""
        lines.append(f"{name}: {value}{tail}")
        probs = answer.get("probabilities")
        if probs:
            ranked = sorted(probs.items(), key=lambda kv: -float(kv[1]))[:3]
            lines.append("    " + ", ".join(f"{k}={float(v):.2f}" for k, v in ranked))
    return "\n".join(lines)


def _background_tab(driver: Any, target: Any) -> None:
    if isinstance(driver, harness.Harness) and not (target.url_hint or "").startswith("target:"):
        tab = driver.open_tab("about:blank")
        target.url_hint = f"target:{tab['id']}"


def tool_browser_read(args: dict[str, Any]) -> str:
    def body(driver: Any, target: Any) -> str:
        if args.get("url"):
            _background_tab(driver, target)
            navigate_and_settle(driver, target, args["url"], 2.0)
        return browser_read(driver, target)

    text, target = with_browser_session(
        args.get("profile"),
        body,
        _port(args),
    )
    if not text:
        return "(the page returned no visible text)"
    limit = max(500, min(int(args.get("max_chars", 6000)), 20000))
    return f"port={target.port} url={target.url}\n\n{text[:limit]}"


def tool_browser_read_many(args: dict[str, Any]) -> str:
    urls = [u.strip() for u in (args.get("urls") or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return "no urls given"
    concurrency = max(1, min(int(args.get("concurrency", DEFAULT_READ_CONCURRENCY)), 8))
    try:
        # The shared session supplies the port/window; the batch builds its own
        # transport per worker, because one transport serialises its calls.
        session, target = _browser_session(args.get("profile"), _port(args))
        new_driver = (
            (lambda: harness.Harness(port=target.port))
            if isinstance(session, harness.Harness)
            else None
        )
        results = browser_read_many(
            target, urls, concurrency=concurrency, new_driver=new_driver
        )
    except DriverError as exc:
        return str(exc)
    limit = max(500, min(int(args.get("max_chars", 6000)), 20000))
    blocks = [f"url={url}\n{text[:limit]}" for url, text in results]
    header = f"{len(results)} page(s), {concurrency} at a time"
    return header + "\n\n" + "\n\n---\n\n".join(blocks)


def tool_browser_script(args: dict[str, Any]) -> str:
    """Run a deterministic BetterWright script without invoking Jev per action."""
    port = _port(args)
    if port is None:
        wanted = args.get("profile")
        chosen, problem = _pick_profile(running_profiles(), wanted)
        if chosen is None or chosen.port is None:
            raise betterwright.BetterWrightError(problem or "no running CDP browser matched the requested profile")
        port = chosen.port
    # Detach our legacy transport, retaining its explicitly chosen workflow tab.
    target = _SESSION.get("target")
    page_url = args.get("page_url")
    if not page_url and target is not None and target.port == port:
        hint = target.url_hint or ""
        if hint.startswith("target:"):
            with betterwright.urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                tabs = json.load(response)
            matches = [t for t in tabs if t.get("id") == hint[7:]]
            if len(matches) != 1:
                raise betterwright.BetterWrightError("Workflow tab disappeared; inspect tabs before continuing")
            page_url = matches[0]["url"]
    reset_browser_session()
    code = str(args.get("code") or "")
    if args.get("dismiss_overlays", True):
        code = betterwright.DISMISS_OVERLAYS_JS + code
    options = {"timeout": float(args.get("timeout", 120))}
    if args.get("dismiss_overlays") is False:
        options["dismiss_overlays"] = False
    if page_url:
        options["page_url"] = page_url
    owner = _GOLOGIN.get("session")
    scope = args.get("checkpoint_scope")
    if not scope and owner is not None and owner.port == port:
        scope = "gologin:" + owner.profile.id
    if scope:
        options["checkpoint_scope"] = str(scope)
    if args.get("submission") is not None:
        options["submission"] = args["submission"]
    result = betterwright.run_script(port, code, **options)
    if not result.get("ok"):
        raise betterwright.BetterWrightError(json.dumps(result, ensure_ascii=False))
    return f"port={port}\n" + json.dumps(result, ensure_ascii=False)


# -- android ----------------------------------------------------------------


def android_device(args: dict[str, Any]) -> Any:
    """Resolve the target phone, or raise AdbError with the fix in the message."""
    return android_engine.pick_device(args.get("serial"))


def tool_android_devices(args: dict[str, Any]) -> str:
    found = android_engine.devices()
    if not found:
        return (
            "no Android device attached.\n"
            "  * plug it in over USB, or `adb connect <ip>:5555` for wireless debugging\n"
            "  * on the phone: Settings > Developer options > USB debugging"
        )

    lines = [f"{len(found)} device(s) attached:"]
    for device in found:
        lines.append(("> " if device.usable else "  ") + device.describe())
    if not any(d.usable for d in found):
        lines.append("")
        lines.append(
            "none usable — unlock the phone and accept the 'Allow USB debugging' prompt"
        )
    return "\n".join(lines)


def tool_android_use(args: dict[str, Any]) -> str:
    max_steps = int(_bounded_number(args, "max_steps", DEFAULT_MAX_STEPS, 1, 20))
    settle = _bounded_number(args, "settle", ANDROID_DEFAULT_SETTLE, 0, 10)
    goal = args["goal"]
    act = bool(args.get("act", False))
    try:
        device = android_device(args)
        result = android_engine.run(
            device.serial,
            goal,
            JevChooser(),
            act=act,
            max_steps=max_steps,
            min_confidence=float(args.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
            settle=settle,
            writer=TextModel(),
            decompose=bool(args.get("decompose", True)),
            cache=PlanCache(CACHE_PATH) if args.get("use_cache", True) else None,
        )
    except android_engine.AdbError as exc:
        return str(exc)

    return f"device={device.serial}\n" + render(result)


def tool_android_read(args: dict[str, Any]) -> str | list[dict[str, Any]]:
    try:
        device = android_device(args)
        observation = android_engine.snapshot(device.serial)
        text = observation.read()
    except android_engine.AdbError as exc:
        return str(exc)

    controls = "\n".join(f"- {target['description']}" for target in observation.targets)
    parts = [f"device={device.serial}", text[:20000] or "(the screen returned no visible text)"]
    if controls:
        parts.append("Tappable controls (use an exact description with android_use; re-read after navigation):\n" + controls)
    text = "\n\n".join(parts)
    if args.get("include_screenshot", False):
        capture = android_engine.exec_out(device.serial, ["screencap", "-p"])
        if not capture.startswith(b"\x89PNG\r\n\x1a\n"):
            raise android_engine.AdbError("Phone screenshot did not return a PNG; inspect the device connection.")
        return [{"type": "text", "text": text}, {"type": "image", "mimeType": "image/png",
                "data": base64.b64encode(capture).decode("ascii")}]
    return text


def tool_android_facebook(args: dict[str, Any]) -> str:
    action = args.get("action")
    if action not in ("accounts", "location", "restart", "audit"):
        raise ValueError("action must be accounts, location, restart, or audit")
    if action == "audit":
        for name in ("timeout", "chunk_budget_seconds"):
            if name in args and isinstance(args[name], bool):
                raise ValueError(f"{name} must be a number")
    timeout = _bounded_number(args, "timeout", facebook_audit.DEFAULT_ACCOUNT_TIMEOUT if action == "audit" else ANDROID_LOCATION_TIMEOUT, 1, 60)
    account = args.get("account")
    if action in ("location", "restart") and (not isinstance(account, str) or not account.strip()):
        raise ValueError(f"account is required for action={action}; use action=accounts first")
    if action == "audit":
        chunk_size = args.get("chunk_size", facebook_audit.DEFAULT_CHUNK_SIZE)
        if isinstance(chunk_size, str) and re.fullmatch(r"[0-9]+", chunk_size.strip()):
            chunk_size = int(chunk_size.strip())
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("chunk_size must be an integer")
        if not 1 <= chunk_size <= facebook_audit.MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be between 1 and {facebook_audit.MAX_CHUNK_SIZE}")
        resume_token = args.get("resume_token")
        if resume_token is not None and not isinstance(resume_token, str):
            raise ValueError("resume_token must be a string")
        if resume_token is not None:
            facebook_audit.validate_resume_token_format(resume_token)
            facebook_audit.validate_resume_token(resume_token)
        def audit_boolean(name: str) -> bool:
            value = args.get(name, False)
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().casefold() in ("true", "false"):
                return value.strip().casefold() == "true"
            raise ValueError(f"{name} must be a boolean or the string 'true'/'false'")
        continue_after_blocker = audit_boolean("continue_after_blocker")
        retry_current = audit_boolean("retry_current")
        if continue_after_blocker and retry_current:
            raise ValueError("continue_after_blocker and retry_current are mutually exclusive")
        if retry_current and resume_token is None:
            raise ValueError("retry_current requires a resume_token")
        chunk_budget = _bounded_number(args, "chunk_budget_seconds", facebook_audit.CHUNK_BUDGET_SECONDS, 10, 55)
        if timeout > chunk_budget:
            raise ValueError("timeout must not exceed chunk_budget_seconds")
        device = android_device(args)
        result = facebook_audit.run(device.serial, timeout=timeout, chunk_size=chunk_size,
                                    resume_token=resume_token, facebook_run=facebook_android.run,
                                    chunk_budget_seconds=chunk_budget,
                                    continue_after_blocker=continue_after_blocker,
                                    retry_current=retry_current)
        return json.dumps(result, ensure_ascii=False)
    device = android_device(args)
    return json.dumps(facebook_android.run(device.serial, action=action, account=account, timeout=timeout), ensure_ascii=False)


def tool_android_facebook_flow(args: dict[str, Any]) -> str:
    """Validate all arguments before device discovery or checkpoint access."""
    serial = args.get("serial")
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise ValueError("serial must be a nonempty string")
    account = args.get("account")
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account is required and must be a nonempty string")
    place = args.get("place", "Manila, Philippines")
    if not isinstance(place, str) or not place.strip():
        raise ValueError("place must be a nonempty string")
    audience = args.get("audience", "Public")
    if audience != "Public":
        raise ValueError("audience must be 'Public'")
    publish = args.get("publish", False)
    if not isinstance(publish, bool):
        raise ValueError("publish must be a boolean")
    run_id = args.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200):
        raise ValueError("run_id must be a nonempty string of at most 200 characters")
    resume_token = args.get("resume_token")
    if publish and run_id is None and resume_token is None:
        raise ValueError("publish=true requires run_id or resume_token")
    timeout = args.get("timeout", 55)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a number from 1 to 60 seconds")
    if not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise ValueError("timeout must be a number from 1 to 60 seconds")
    if resume_token is not None:
        if not isinstance(resume_token, str):
            raise ValueError("resume_token must be a string")
        facebook_flow_state.validate_token_format(resume_token)
        checkpoint = facebook_flow_state.load(resume_token)
        if any(checkpoint[key] != value for key, value in
               (("account", account), ("place", place), ("audience", audience))):
            raise ValueError("resume_token is bound to a different account, place, or audience")
        if serial is not None and checkpoint["serial"] != serial:
            raise ValueError("resume_token is bound to a different device")

    device = android_device(args)
    result = facebook_flow.run(device.serial, account=account, place=place,
                               audience=audience, resume_token=resume_token,
                               timeout=float(timeout), publish=publish,
                               run_id=run_id)
    return json.dumps(result, ensure_ascii=False)


def tool_android_location(args: dict[str, Any]) -> str:
    timeout = _bounded_number(args, "timeout", ANDROID_LOCATION_TIMEOUT, 0, 60)
    try:
        device = android_device(args)
        found = android_engine.account_location(
            device.serial,
            timeout=timeout,
        )
    except android_engine.AdbError as exc:
        return str(exc)

    lines = [found.describe()]
    if not found.location:
        # Nothing was parsed, so hand the caller the page's own words rather than a
        # bare "(none)" — a sign-in screen and a half-drawn one read very
        # differently and only the text tells them apart.
        text = found.text.strip()
        if text:
            lines += ["", "shows:", text[:2000]]
    return "\n".join(lines)


HANDLERS = {
    "browser_profiles": tool_browser_profiles,
    "browser_open": tool_browser_open,
    "browser_close": tool_browser_close,
    "browser_use": tool_browser_use,
    "browser_action": tool_browser_action,
    "browser_script": tool_browser_script,
    "browser_extract": tool_browser_extract,
    "browser_read": tool_browser_read,
    "browser_read_many": tool_browser_read_many,
    "android_devices": tool_android_devices,
    "android_use": tool_android_use,
    "android_read": tool_android_read,
    "android_location": tool_android_location,
    "android_facebook": tool_android_facebook,
    "android_facebook_flow": tool_android_facebook_flow,
}


# -- JSON-RPC over stdio ----------------------------------------------------


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        version = requested if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": PROMPTS}}

    if method == "prompts/get":
        params = request.get("params") or {}
        payload = prompt_messages(params.get("name", ""), params.get("arguments") or {})
        if payload is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown prompt {params.get('name')!r}"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"unknown tool {name!r}"}],
                    "isError": True,
                },
            }
        try:
            text = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": text if isinstance(text, list) else [{"type": "text", "text": text}]},
            }
        except (DriverError, betterwright.BetterWrightError) as exc:
            # A driver refusal is an operational message, not a crash: return it as
            # text so the caller acts on it instead of seeing a stack trace.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        except android_engine.AdbError as exc:
            # Same contract for the phone: "no device attached" and "unauthorized"
            # are instructions, not exceptions.
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        except Exception as exc:  # noqa: BLE001 - surface everything to the caller
            log(traceback.format_exc())
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if request_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    load_env()
    if not os.environ.get("TYPESAFE_API_KEY"):
        log("warning: TYPESAFE_API_KEY is not set — browser_use will fail at the first decision")
    log(f"{SERVER_NAME} {SERVER_VERSION} ready (browser only)")

    busy = threading.Event()
    active_request = {}
    write_lock = threading.Lock()

    def respond(request):
        response = None
        try:
            response = handle(request)
        finally:
            # Release ownership before publishing completion. A client may send
            # its next call as soon as it receives the response; it must not see
            # the completed call as still busy. The same lock also protects the
            # busy check below from observing a stale state while sending waits.
            with write_lock:
                busy.clear()
                if response is not None:
                    send(response)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("method") == "tools/call":
            with write_lock:
                if busy.is_set():
                    send({"jsonrpc": "2.0", "id": request.get("id"), "result": {
                        "isError": True, "content": [{"type": "text", "text": f"Request not started: {active_request.get('tool', 'another tool')} (request ID {active_request.get('id')}) is still running. Wait for that original call's result; do not issue parallel phone calls or repeat actions."}]}})
                else:
                    active_request.update(tool=request.get("params", {}).get("name", "unknown tool"), id=request.get("id"))
                    busy.set()
                    threading.Thread(target=respond, args=(request,), daemon=True).start()
        else:
            response = handle(request)
            if response is not None:
                with write_lock:
                    send(response)
    betterwright.close_sessions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
