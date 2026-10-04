"""GoLogin in its original form: GoLogin launches the profile, we attach.

A GoLogin profile is **not** a Chrome profile copy. It lives in GoLogin's own
store with its fingerprint, proxy, cookies and geolocation, and it is meant to be
started by GoLogin's own launcher — Orbita, their Chromium fork — which is what
gives it that identity. Copying the user-data directory and starting plain Chrome,
the way `profiles.start_profile` does for Chrome, would start a different browser
with a different fingerprint and defeat the whole point of using GoLogin.

So this module does not copy anything. It hands the profile to GoLogin's official
Python SDK, which downloads the profile, writes its configuration, starts Orbita
with a remote-debugging port, and returns the CDP debugger address. All this
engine needs is that address: `:PORT` is what the rest of the browser layer
already knows how to attach to.

Two things worth knowing about the SDK, both handled here:

* it **prints to stdout** in places, and stdout is our JSON-RPC channel — every
  SDK call is wrapped so its output goes to stderr instead;
* it **phones home** (Sentry) unless `DISABLE_TELEMETRY` is set — we set it, because
  the user asked us to drive their browser, not to opt them into telemetry.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

API_URL = "https://api.gologin.com"

#: Token names, in the order they win. `GOLOGIN_TOKEN` is what GoLogin's own tools
#: read; the prefixed one lets a user keep a jev-use-only token.
TOKEN_NAMES = ("GOLOGIN_TOKEN", "JEV_USE_GOLOGIN_TOKEN")

#: "127.0.0.1:53142" (local) or "wss://host:443/devtools/browser/…" (cloud).
_PORT_IN_ADDRESS = re.compile(r":(\d{2,5})(?:[/\s]|$)")


class GoLoginError(RuntimeError):
    """Anything that stops us launching or finding a GoLogin profile, with a fix."""


def token() -> str | None:
    for name in TOKEN_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def sdk_installed() -> bool:
    """Whether the official GoLogin SDK is importable (not whether it works)."""
    try:
        import gologin  # noqa: F401
    except Exception:
        return False
    return True


def _require_token() -> str:
    found = token()
    if found:
        return found
    raise GoLoginError(
        "no GoLogin API token. Add one with:\n"
        "    jev-use install --gologin-token=<token>\n"
        "Get it from https://app.gologin.com/#/personalArea/TokenApi"
    )


def _require_sdk() -> None:
    if sdk_installed():
        return
    raise GoLoginError(
        "the GoLogin SDK is not installed, and it is what launches a GoLogin "
        "profile in its original form. Install it with:\n"
        "    jev-use install --gologin-token=<token>\n"
        "(or: pip install gologin)"
    )


# -- listing profiles (plain HTTP; no SDK needed) ---------------------------


@dataclass
class GoLoginProfile:
    id: str
    name: str

    def describe(self) -> str:
        return f"{self.name!r} ({self.id})"


def _api_get(path: str) -> Any:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        headers={
            "Authorization": f"Bearer {_require_token()}",
            "User-Agent": "jev-use",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GoLoginError(
                "GoLogin rejected the API token (401/403). Check it at "
                "https://app.gologin.com/#/personalArea/TokenApi"
            ) from exc
        raise GoLoginError(f"GoLogin API {path} -> {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GoLoginError(f"could not reach the GoLogin API: {exc}") from exc


def profiles() -> list[GoLoginProfile]:
    """Every profile on the GoLogin account.

    `/browser/v2` has answered with both a bare list and a `{"profiles": […]}` wrapper
    across versions, so both are accepted rather than betting on one.
    """
    found: list[GoLoginProfile] = []
    seen: set[str] = set()
    for page_number in range(1, 1001):
        path = "/browser/v2" if page_number == 1 else f"/browser/v2?page={page_number}"
        payload = _api_get(path)
        entries = payload.get("profiles") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise GoLoginError(f"GoLogin profile page {page_number} returned an unexpected response shape")
        added = 0
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            profile_id = str(entry["id"])
            if profile_id in seen:
                continue
            seen.add(profile_id)
            added += 1
            found.append(GoLoginProfile(id=profile_id, name=str(entry.get("name") or profile_id)))
        if len(entries) < 30:
            return found
        if not added:
            raise GoLoginError(f"GoLogin profile pagination repeated page {page_number}; cannot claim the list is complete")
    raise GoLoginError("GoLogin profile pagination exceeded 1000 pages; cannot claim the list is complete")


def find(wanted: str) -> GoLoginProfile:
    """Resolve a profile by id or display name, the way `browser_open` takes one."""
    available = profiles()
    if not available:
        raise GoLoginError("the GoLogin account has no profiles")
    needle = wanted.lower()
    exact = [p for p in available if needle in (p.name.lower(), p.id.lower())]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in available if needle in p.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(sorted({p.name for p in (exact or partial)}))
        raise GoLoginError(f"{wanted!r} matches several GoLogin profiles: {names}")
    names = ", ".join(sorted(p.name for p in available)[:20])
    raise GoLoginError(f"no GoLogin profile matches {wanted!r}. Available: {names}")


# -- launching --------------------------------------------------------------


def port_from_address(address: str) -> int:
    """The port out of a debugger address, local or cloud.

    Local: `127.0.0.1:53142`. Cloud: `wss://<id>.orbita.gologin.com/devtools/…` —
    which has no local port and so cannot be attached to through `cdp_port`; that
    is reported rather than guessed at.
    """
    match = _PORT_IN_ADDRESS.search(address or "")
    if not match:
        raise GoLoginError(
            f"could not read a port from the GoLogin debugger address {address!r}. "
            "If this is a cloud profile (wss://…orbita.gologin.com), it cannot be "
            "attached to from here — use a local profile."
        )
    return int(match.group(1))


@dataclass
class Session:
    """A GoLogin profile this process started, and how to stop it.

    `stop()` is not optional housekeeping: the SDK commits the profile — cookies,
    local storage, the logged-in state — back to GoLogin when it stops. Kill the
    browser without stopping and the next launch re-downloads the profile, so the
    session's work is lost.
    """

    profile: GoLoginProfile
    port: int
    debugger: str
    _handle: Any = field(default=None, repr=False)

    def stop(self) -> None:
        if self._handle is None:
            return
        with _capture_stdout():
            self._handle.stop()
        self._handle = None


@contextlib.contextmanager
def _capture_stdout():
    """Run third-party code whose `print` must not reach our JSON-RPC channel.

    The GoLogin SDK prints in several places (telemetry setup, stop messages). On
    stdout that is not noise, it is stream corruption — the harness sees a broken
    JSON-RPC frame and the tool call dies with a parse error nowhere near the cause.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def launch(
    profile: GoLoginProfile,
    *,
    port: int | None = None,
    url: str | None = None,
    headless: bool = False,
) -> Session:
    """Start the profile in its original form and return the CDP endpoint.

    Blocking: the SDK downloads the profile (and Orbita, the first time) and waits
    for the debugger to answer, which can take a while on a cold profile. That is
    the price of a real GoLogin browser rather than a copied Chrome.
    """
    _require_sdk()
    # Before importing the SDK: its constructor initialises Sentry unless this is set.
    os.environ.setdefault("DISABLE_TELEMETRY", "true")

    from gologin import GoLogin  # imported late so the dependency stays optional

    options: dict[str, Any] = {"token": _require_token(), "profile_id": profile.id}
    if port:
        options["port"] = port
    extra: list[str] = []
    if headless:
        extra.append("--headless")
    if url:
        # Orbita is Chromium: a trailing URL is opened, exactly as for Chrome.
        extra.append(url)
    if extra:
        options["extra_params"] = extra

    with _capture_stdout():
        handle = GoLogin(options)
        debugger = handle.start()

    if not isinstance(debugger, str) or not debugger:
        raise GoLoginError(f"GoLogin did not return a debugger address ({debugger!r})")
    return Session(
        profile=profile,
        port=port_from_address(debugger),
        debugger=debugger,
        _handle=handle,
    )
