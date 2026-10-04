"""Android use driven by Jev, over ADB.

Why ADB and not the desktop surface
-----------------------------------
scrcpy is a *viewer*. It renders the phone into one OpenGL surface, so anything
looking at it through accessibility or the window tree sees a single opaque
element — the same dead end that made the GNOME/Wayland desktop path unusable.
The automation channel underneath scrcpy is ADB, and that is what this speaks.

Two consequences worth stating:

* **scrcpy is not required.** This works with the phone on USB and adb running,
  scrcpy open or not. (It also means you can leave scrcpy open and *watch* the
  agent work, which is the pleasant version.)
* **Coordinates need no scaling.** `uiautomator` reports bounds in the device's
  own space and `input tap` consumes the same space, so a tap is arithmetic
  rather than a mapping problem.

The snapshot primitive is `uiautomator dump`: an XML tree of the live view
hierarchy with text, content descriptions, classes and bounds. That is the
Android equivalent of the browser engine's DOM snapshot, and it is what makes
this design work — the model still chooses from a closed set of elements this
server enumerated, never from a bare coordinate.

Measured on a Huawei RNE-L21 (EMUI), because the obvious path is wrong there:
`/sdcard` accepts the dump and then does not have the file, so the dump is
written to `/data/local/tmp`, which always exists and needs no permissions.
"""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .choosers import Decision, deterministic_decision as chooser_deterministic_decision, validate

ADB = "adb"
DUMP_PATH = "/data/local/tmp/jev_dump.xml"
ADB_TIMEOUT = 30.0
DUMP_TIMEOUT = 25.0
SCROLL_PIXELS = 700

#: `uiautomator dump` refuses while the UI is animating. It is not a real
#: failure and the next attempt usually succeeds, so it is retried rather than
#: reported.
DUMP_RETRIES = 3

#: A harvested row label is truncated here. A row's title and its one-line
#: summary are worth having; its whole subtree is not.
LABEL_LIMIT = 80

#: Cap on the option set. Past this the choice stops being meaningful and the
#: confidence score stops being readable — the same reason the browser engine
#: caps its candidates.
MAX_CANDIDATES = 120

#: How long to wait for the screen to move after acting.
#:
#: Deliberately half the browser engine's 3 s, and the reason is measured. On
#: Android most actions navigate *within* an app — tapping a row, opening a tab,
#: scrolling — so the foreground app does not change and the cheap settle poll
#: cannot fire. A long settle is then pure dead time: the poll waits for a signal
#: that is never coming.
#:
#: What actually settles the screen is the dump itself, which blocks on UI idle:
#: measured, a dump taken right after launching an app took 3.0-3.4 s versus
#: 2.1 s on a still screen. So this value is a floor guard for the poll, not the
#: settling mechanism, and it can safely be short.
DEFAULT_SETTLE = 1.5

ANDROID_OPERATIONS = {
    "click_element": "Tap exactly one of the listed elements",
    "type_text": "Write a value into one of the listed text fields",
    "scroll_down": "The target is further down, swipe up to reveal it",
    "scroll_up": "The target is further up, swipe down to reveal it",
    "go_back": "Press the system Back button to leave this screen",
    "go_home": "Press the system Home button",
    "wait": "The screen is still loading or animating",
    "done": "The screen already shows the goal satisfied",
    "impossible": "Nothing on this screen can make progress toward the goal",
}

#: `navigate` is added only when the goal names a URL, so it is never a free
#: choice — the same rule the browser engine applies.
NAVIGATE_OPERATION = {"navigate": "Open the URL the goal names"}

#: Known TLDs. Requiring one is what keeps a bare domain (`whoer.net`) from being
#: confused with the many other dotted tokens in a goal — a package name, a
#: version number, a filename. The scheme form is accepted unconditionally.
TLDS = frozenset(
    """
    com net org io dev ai co me app xyz info biz gov edu int mil
    uk de fr ru in us ca au jp cn br nl se no es it ch pl be at dk fi cz pt gr
    tr kr tw hk sg mx ar za ng pk bd id ph vn th my ir sa ae il nz ie hu ro bg
    """.split()
)

_SCHEMED_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_BARE_DOMAIN = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,})(?:/[^\s\"'<>]*)?\b", re.I
)


def url_in(goal: str) -> str | None:
    """Pull a URL out of the goal, in code and never from the model.

    Accepts `https://whoer.net` and the bare `whoer.net` people actually type,
    because a goal is written by a person and "go on whoer.net" is how a person
    writes it. The TLD has to be a real one, which is what stops `com.whatsapp`
    or `3.36` from being read as a destination.

    A scheme that is missing is filled in rather than guessed at: `whoer.net`
    becomes `https://whoer.net`, which is what a browser would do.
    """
    text = goal or ""
    match = _SCHEMED_URL.search(text)
    if match:
        return match.group(0)

    for candidate in _BARE_DOMAIN.finditer(text):
        if candidate.group(1).lower() in TLDS:
            return f"https://{candidate.group(0)}"
    return None

#: Key codes for the two navigation operations. BACK is the single most useful
#: action on Android — far more of the UI is reachable by backing out of a
#: screen than by any element on it.
KEYCODES = {"go_back": 4, "go_home": 3}

_EXACT_TAP_GOAL = re.compile(r"^(?:please\s+)?(?:tap|click)\s+(.+)$", re.I)


def deterministic_decision(goal: str, observation: Any) -> Decision | None:
    """Resolve an explicit observed Android target before global key aliases.

    Android can show a tab named ``Home`` while the system Home key is also a
    valid action. An exact "tap <observed description>" names the UI target, so
    match that closed-set description first. Other goals, including bare
    "go home", retain the shared chooser's existing deterministic rules.
    """
    match = _EXACT_TAP_GOAL.fullmatch((goal or "").strip())
    if match:
        if "click_element" not in observation.operations():
            return Decision(kind="click_element", confidence=1.0,
                            source="deterministic",
                            rejection="click_element is unavailable on this screen")
        wanted = re.sub(r"\s+", " ", match.group(1).strip()).casefold()
        if wanted:
            targets = observation.targets_for("click_element")
            exact = [target for target in targets
                     if wanted in {
                         re.sub(r"\s+", " ", str(target.get(key, "")).strip()).casefold()
                         for key in ("label", "description")
                     }]
            if len(exact) == 1:
                return Decision(kind="click_element", element_id=exact[0]["id"],
                                confidence=1.0, source="deterministic")
            if len(exact) != 1:
                reason = ("the exact Android target is ambiguous" if exact
                          else "no exact Android target matches the requested description")
                return Decision(kind="click_element", confidence=1.0,
                                source="deterministic",
                                rejection=reason)
        return Decision(kind="click_element", confidence=1.0,
                        source="deterministic",
                        rejection="the exact Android target description is empty")
    return chooser_deterministic_decision(goal, observation)

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

#: Classes that mean "you can type here". Matching on the class name rather than
#: a `fillable` attribute is necessary: `uiautomator` does not label editability,
#: and `EditText` is stable across app frameworks in a way resource ids are not.
EDITABLE_CLASSES = ("EditText", "AutoCompleteTextView", "SearchView", "SearchAutoComplete")


class AdbError(RuntimeError):
    """Raised when adb cannot be reached or returns an error."""


# -- adb --------------------------------------------------------------------


def adb(serial: str | None, args: list[str], *, timeout: float = ADB_TIMEOUT, binary: bool = False):
    """Run one adb command. Absolute binary is not assumed on PATH everywhere."""
    command = [ADB]
    if serial:
        command += ["-s", serial]
    command += args
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=timeout, text=not binary
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdbError(f"could not run adb: {exc}") from exc
    return result


def shell(serial: str | None, command: str, *, timeout: float = ADB_TIMEOUT) -> str:
    """`adb shell <command>` as a single remote command string.

    A single string, not a list: `adb shell` concatenates its arguments and hands
    the result to the *device* shell, so anything with a quote or a space in it
    has to be quoted here rather than relied on to survive transport.
    """
    result = adb(serial, ["shell", command], timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AdbError(f"adb shell failed ({result.returncode}): {detail[:300]}")
    return result.stdout


def exec_out(serial: str | None, args: list[str], *, timeout: float = ADB_TIMEOUT) -> bytes:
    """`adb exec-out` — binary-safe, no CRLF translation.

    `adb shell cat` mangles line endings and is not safe for a screenshot;
    `exec-out` is the correct tool for both the dump and `screencap`.
    """
    result = adb(serial, ["exec-out", *args], timeout=timeout, binary=True)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode(errors="replace").strip()
        raise AdbError(f"adb exec-out failed: {detail[:300]}")
    return result.stdout or b""


# -- discovery --------------------------------------------------------------


@dataclass
class Device:
    serial: str
    state: str  # "device", "unauthorized", "offline"
    model: str = ""
    product: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "device"

    def describe(self) -> str:
        label = self.model or self.product or "unknown model"
        return f"{self.serial}  {label}  [{self.state}]"


def devices() -> list[Device]:
    """Every attached device, in `adb devices` order.

    Unauthorized and offline devices are returned too, on purpose: "the phone is
    plugged in but you have not accepted the debugging prompt" is the single most
    common failure and it is invisible if you filter to usable devices only.
    """
    result = adb(None, ["devices", "-l"])
    out = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode()
    found: list[Device] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        extras = dict(
            token.split(":", 1) for token in parts[2:] if ":" in token
        )
        found.append(
            Device(
                serial=serial,
                state=state,
                model=extras.get("model", ""),
                product=extras.get("product", ""),
            )
        )
    return found


def pick_device(serial: str | None = None) -> Device:
    """Resolve the device to drive, or explain exactly what to do.

    Attaching to a different phone than the one asked for is refused rather than
    silently done — the same rule the browser engine applies to Chrome profiles,
    and for the same reason: the wrong device answers every question wrongly.
    """
    found = devices()
    if not found:
        raise AdbError(
            "no Android device attached.\n"
            "  * plug it in over USB, or `adb connect <ip>:5555` for wireless debugging\n"
            "  * on the phone: Settings > Developer options > USB debugging"
        )

    if serial:
        match = [d for d in found if d.serial == serial]
        if not match:
            available = "\n".join(f"    {d.describe()}" for d in found)
            raise AdbError(f"no device with serial {serial!r}.\nAttached:\n{available}")
        chosen = match[0]
    else:
        usable = [d for d in found if d.usable]
        if len(usable) > 1:
            available = "\n".join(f"    {d.describe()}" for d in usable)
            raise AdbError(
                "more than one device is attached; name the one to use:\n" + available
            )
        chosen = usable[0] if usable else found[0]

    if not chosen.usable:
        raise AdbError(
            f"device {chosen.serial} is {chosen.state!r}, not usable.\n"
            + (
                "  it is unauthorized: unlock the phone and accept the "
                "'Allow USB debugging' prompt."
                if chosen.state == "unauthorized"
                else "  check that the cable still carries data."
            )
        )
    return chosen


def screen_size(serial: str) -> tuple[int, int]:
    """The live display size, for position hints and swipe distances.

    Prefers an `Override size` when one is set: that is what the device is
    actually rendering at, and the dump's bounds follow it.

    This always asks the device. `snapshot()` uses `cached_screen_size` instead,
    because measured against a real device this call costs **800 ms** — more than
    a quarter of the old per-snapshot budget — for a value that does not change
    between two screens unless the display size or the orientation does.
    """
    text = shell(serial, "wm size")
    override = re.search(r"Override size:\s*(\d+)x(\d+)", text)
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", text)
    match = override or physical
    if not match:
        return (1080, 1920)
    return (int(match.group(1)), int(match.group(2)))


#: serial -> (size, rotation). Deliberately tiny and process-local: it exists so
#: one decision step does not pay 800 ms for a constant.
_SCREEN_CACHE: dict[str, tuple[tuple[int, int], str]] = {}

_ROTATION = re.compile(r'rotation="(\d+)"')


def rotation_of(xml_text: str) -> str:
    """The rotation the hierarchy was captured at.

    Free: it is an attribute of the dump we already have. Using it as the cache
    key means a rotated device cannot be given stale dimensions, so caching costs
    nothing in correctness — which is the only reason it is acceptable at all.
    """
    match = _ROTATION.search(xml_text[:400])
    return match.group(1) if match else ""


def cached_screen_size(serial: str, rotation: str = "") -> tuple[int, int]:
    """`screen_size`, memoised until the rotation changes."""
    hit = _SCREEN_CACHE.get(serial)
    if hit is not None and hit[1] == rotation:
        return hit[0]
    size = screen_size(serial)
    _SCREEN_CACHE[serial] = (size, rotation)
    return size


def forget_screen_size(serial: str | None = None) -> None:
    """Drop the memo. For tests, and for a caller that changed the display size."""
    if serial is None:
        _SCREEN_CACHE.clear()
    else:
        _SCREEN_CACHE.pop(serial, None)


def foreground(serial: str) -> str:
    """The focused app, e.g. `com.huawei.android.launcher/.unihome.UniHomeLauncher`.

    This is the Android analogue of the page URL: it is what the run report
    quotes and what the plan cache keys on, so the same goal on a different
    screen cannot replay the wrong plan.
    """
    try:
        text = shell(serial, "dumpsys window")
    except AdbError:
        return ""
    match = re.search(r"mFocusedApp=.*?\s([\w.]+/[\w.$]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"mCurrentFocus=Window\{[^}]*\s([\w.]+/[\w.$]+)", text)
    return match.group(1) if match else ""


def focus_signature(serial: str) -> str:
    """A cheap "has the window changed?" signal, from the app and the focus window.

    It comes out of the SAME single `dumpsys window` call `foreground()` already
    makes (~52 ms measured), so it adds no adb traffic, and it moves for what the
    app string alone misses: a dialog, a menu or a new surface gets its own focused
    window.

    **What it does not see.** An ordinary tab, fragment or view swap inside one
    activity keeps the same task, the same app AND the same focused window, so it is
    invisible here — the earlier "sees in-app tabs" claim was wrong and the test that
    pretended otherwise was fabricating a new window. Detecting those cheaply is not
    possible: it needs the hierarchy, which is a ~2.1 s dump. That case is guarded at
    replay time instead, by refusing targetless plans and by resolving every stored
    description against the fresh screen.

    **Unavailable is `""`.** An `adb` failure, or a `dumpsys` we could not parse,
    returns the empty string, which callers must treat as UNKNOWN — never as a
    transition or a usable cache key. `_screen_moved` is the only sanctioned
    comparison for exactly that reason: `"" -> "app/.Main"` is a device that was
    momentarily unreadable, not a screen that changed. Returning `"|"` for a parsed
    but unrecognised dump would be worse than empty: it is truthy, so every such
    screen would share one cache key.
    """
    try:
        text = shell(serial, "dumpsys window")
    except AdbError:
        return ""
    app = re.search(r"mFocusedApp=.*?\s([\w.]+/[\w.$]+)", text)
    if not app:
        return ""
    # `u\d+` not `u0`: a work profile runs as user 10 (and clones as 999), and a
    # hardcoded u0 read those screens as having no focused window at all.
    focus = re.search(r"mCurrentFocus=Window\{[0-9a-f]+ u\d+ ([^}]+)\}", text)
    return f"{app.group(1)}|{focus.group(1) if focus else ''}"


def _screen_moved(before: str, now: str) -> bool:
    """True only when two REAL window signatures differ.

    An empty signature means the device could not be read; treating that as a move
    would make a momentary adb failure look like the screen had changed and cut a
    settle short.
    """
    return bool(before) and bool(now) and now != before


# -- the view hierarchy -----------------------------------------------------


@dataclass
class Node:
    ref: int
    cls: str
    package: str
    text: str
    content_desc: str
    resource_id: str
    bounds: tuple[int, int, int, int]  # x1, y1, x2, y2
    clickable: bool
    scrollable: bool
    long_clickable: bool
    checkable: bool
    checked: bool
    enabled: bool
    password: bool
    editable: bool = False
    #: Label harvested from non-interactive children — see `derive_label`.
    derived: str = ""

    @property
    def centre(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    def label(self) -> str:
        return (self.text or self.content_desc or self.derived or "").strip()


def _attr(element: ET.Element, name: str, default: str = "") -> str:
    return element.attrib.get(name, default) or default


def _parse_bounds(raw: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS.search(raw or "")
    if not match:
        return None
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def derive_label(element: ET.Element, index: dict[int, Node]) -> str:
    """Harvest a label from a node's non-interactive descendants.

    This is the single most important thing the code does for real-world Android,
    and it was found by pointing it at a Settings screen rather than at a fixture.

    On Android a tappable list row is usually a bare container — a `linearlayout`
    or `framelayout` with no text of its own — and the visible words live in a
    child `TextView` that is *not* clickable. So without this, every row on every
    Settings screen, chat list and inbox is offered to the model as
    `linearlayout - middle-centre - at 540,939`, and the model is asked which of
    nineteen identical-looking boxes opens Battery. It cannot know. Measured: Jev
    answered **0.17**, under the threshold, and refused — correctly, because the
    information was not in the option set. The text was on screen; it just was not
    attached to anything tappable.

    Only non-interactive descendants contribute. A container must not absorb the
    label of a clickable child, or every wrapper would become a duplicate of the
    row inside it and the option set would fill with near-identical entries.
    """
    parts: list[str] = []
    for child in element.iter("node"):
        if child is element:
            continue
        child_node = index.get(id(child))
        if child_node is not None and child_node.password:
            continue
        if child_node is not None and (child_node.clickable or child_node.editable):
            continue
        text = (_attr(child, "text") or _attr(child, "content-desc")).strip()
        if text and text not in parts:
            parts.append(text)
        if len(", ".join(parts)) >= LABEL_LIMIT:
            break
    return ", ".join(parts)[:LABEL_LIMIT]


def parse_nodes(xml_text: str) -> list[Node]:
    """Flatten the `uiautomator` XML into a list of Nodes, in document order.

    Document order matters: it is the painting order, so a later node is drawn on
    top of an earlier one and refs stay stable across two dumps of a still screen.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AdbError(f"uiautomator returned unparseable XML: {exc}") from exc

    pairs: list[tuple[ET.Element, Node]] = []
    for element in root.iter("node"):
        if element.tag != "node":
            continue
        bounds = _parse_bounds(_attr(element, "bounds"))
        if bounds is None:
            continue
        cls = _attr(element, "class")
        pairs.append(
            (
                element,
                Node(
                    ref=len(pairs),
                    cls=cls,
                    package=_attr(element, "package"),
                    text=_attr(element, "text"),
                    content_desc=_attr(element, "content-desc"),
                    resource_id=_attr(element, "resource-id"),
                    bounds=bounds,
                    clickable=_attr(element, "clickable") == "true",
                    scrollable=_attr(element, "scrollable") == "true",
                    long_clickable=_attr(element, "long-clickable") == "true",
                    checkable=_attr(element, "checkable") == "true",
                    checked=_attr(element, "checked") == "true",
                    enabled=_attr(element, "enabled") != "false",
                    password=_attr(element, "password") == "true",
                    editable=any(name in cls for name in EDITABLE_CLASSES),
                ),
            )
        )

    # Only interactive nodes need a harvested label: a plain TextView is described
    # by its own text, and walking its subtree would be wasted work.
    index = {id(element): node for element, node in pairs}
    for element, node in pairs:
        if node.label() or not (node.clickable or node.editable or node.checkable):
            continue
        node.derived = derive_label(element, index)

    return [node for _, node in pairs]


def dump(serial: str, *, retries: int = DUMP_RETRIES) -> str:
    """The live view hierarchy as XML.

    Written to `/data/local/tmp` and read back with `exec-out`. `/sdcard` is the
    documented location and is wrong on EMUI: `uiautomator dump` reports success
    and the file is not there, so a naive implementation reads an empty screen
    and reports it as a blank UI.
    """
    last = ""
    for attempt in range(max(1, retries)):
        try:
            shell(serial, f"uiautomator dump {DUMP_PATH}", timeout=DUMP_TIMEOUT)
            xml_text = exec_out(serial, ["cat", DUMP_PATH], timeout=DUMP_TIMEOUT).decode(
                "utf-8", errors="replace"
            )
        except AdbError as exc:
            last = str(exc)
            xml_text = ""

        if xml_text.lstrip().startswith("<?xml"):
            return xml_text
        last = last or "uiautomator produced no XML"
        time.sleep(0.5)  # the usual cause is a mid-animation UI refusing to dump

    raise AdbError(
        f"could not read the view hierarchy: {last}\n"
        "  a screen mid-animation refuses to dump; retrying usually works"
    )


# -- candidates -------------------------------------------------------------


def short_class(cls: str) -> str:
    return cls.rsplit(".", 1)[-1] if cls else "element"


def role_of(node: Node) -> str:
    """A readable role, with the few names that carry real meaning made explicit."""
    name = short_class(node.cls)
    if node.editable:
        return "text field"
    return {
        "Button": "button",
        "ImageButton": "icon button",
        "TextView": "text",
        "ImageView": "image",
        "CheckBox": "checkbox",
        "Switch": "switch",
        "RadioButton": "radio",
        "ToggleButton": "toggle",
        "RecyclerView": "list",
        "ListView": "list",
        "ScrollView": "scroll area",
        "EditText": "text field",
        "Image": "image",
        "Tab": "tab",
    }.get(name, name.lower() if name else "element")


#: A clickable container with no text at all is usually an invisible layout
#: wrapper. Offering those would swamp the option set with indistinguishable
#: entries, so an unlabelled node has to be big enough to be a real target.
MIN_UNLABELLED_AREA_FRACTION = 0.02


def is_candidate(node: Node, screen: tuple[int, int]) -> bool:
    if not node.enabled:
        return False
    if node.width < 4 or node.height < 4:
        return False
    if not (node.clickable or node.editable or node.scrollable or node.checkable):
        return False
    if node.label() or node.resource_id:
        return True
    width, height = screen
    return (node.width * node.height) >= (width * height * MIN_UNLABELLED_AREA_FRACTION)


def position_hint(node: Node, screen: tuple[int, int]) -> str:
    """A coarse, stable descriptor used only to break a label tie.

    Jev's confidence measures how concentrated the distribution is, so two
    candidates that read identically always look like uncertainty. Naming where
    each one is makes the choice separable without teaching it anything about
    any particular app.
    """
    width, height = screen
    if not width or not height:
        return ""
    cx, cy = node.centre
    horizontal = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "centre"
    vertical = "top" if cy < height / 3 else "bottom" if cy > 2 * height / 3 else "middle"
    return f"{vertical}-{horizontal}"


def describe(node: Node) -> str:
    label = "[password field]" if node.password else node.label()
    text = f'{role_of(node)} "{label}"' if label else role_of(node)
    if node.password:
        # Never echo what is in a password field, and say so: the model needs to
        # know it is filled without being told the value.
        text += " (masked)"
    elif node.checked:
        text += " (checked)"
    return text


def dedupe_by_bounds(nodes: list[Node]) -> list[Node]:
    """Keep one candidate per distinct rectangle, preferring a labelled one.

    Identical bounds means identical tap coordinates, so two candidates drawn on
    the same pixels are not two options — they are the *same action offered
    twice*. Nested clickable containers produce exactly this (a FrameLayout
    wrapping a LinearLayout, both clickable, on the Settings search row). Offering
    both cannot be resolved by choosing better; there is nothing to choose
    between, and the model spends its confidence on a distinction that does not
    exist.

    Where one of the group carries a label and the others do not, the label is
    what makes the row legible, so it wins regardless of document order.
    """
    groups: dict[tuple[int, int, int, int], list[Node]] = {}
    order: list[tuple[int, int, int, int]] = []
    for node in nodes:
        if node.bounds not in groups:
            groups[node.bounds] = []
            order.append(node.bounds)
        groups[node.bounds].append(node)

    kept: list[Node] = []
    for bounds in order:
        group = groups[bounds]
        labelled = [n for n in group if n.label()]
        kept.append((labelled or group)[0])
    return kept


def unique_descriptions(chosen: list[Node], screen: tuple[int, int]) -> list[str]:
    """Give every candidate a description no other candidate shares.

    Mutually exclusive options are a hard requirement, not a nicety: Jev's
    confidence measures how concentrated the distribution is, so two entries that
    read the same always look like doubt.

    Measured on a real device, which is why this escalates three times. A lock
    screen offered two stacked cards in the same region, both rendered as
    `framelayout - middle-centre`; Jev answered **0.37**, under the threshold, and
    the run refused to act. The ambiguity *was* the uncertainty — the model was
    not unsure about the goal, it was unsure which of two identical options it was
    being asked about.

    So: label it by position, then by the exact tap point, then by its ref — which
    is unique by construction and therefore a guarantee rather than a hope.
    """
    base = [describe(node) for node in chosen]
    counts = Counter(base)

    narrowed: list[str] = []
    for node, text in zip(chosen, base):
        if counts[text] > 1:
            hint = position_hint(node, screen)
            narrowed.append(f"{text} - {hint}" if hint else text)
        else:
            narrowed.append(text)

    pointed = []
    again = Counter(narrowed)
    for node, text in zip(chosen, narrowed):
        pointed.append(
            f"{text} - at {node.centre[0]},{node.centre[1]}" if again[text] > 1 else text
        )

    final = Counter(pointed)
    return [
        f"{text} (element {node.ref})" if final[text] > 1 else text
        for node, text in zip(chosen, pointed)
    ]


def build_candidates(nodes: list[Node], screen: tuple[int, int]) -> list[dict[str, Any]]:
    chosen = dedupe_by_bounds(
        [n for n in nodes if is_candidate(n, screen)]
    )[:MAX_CANDIDATES]
    descriptions = unique_descriptions(chosen, screen)

    return [
        {
            "id": str(node.ref),
            "role": role_of(node),
            "label": "[password field]" if node.password else node.label(),
            "bounds": node.bounds,
            "centred": node.centre,
            "package": node.package,
            "resource_id": node.resource_id,
            "clickable": bool(node.clickable or node.long_clickable or node.checkable),
            "fillable": bool(node.editable),
            "frame": None,
            "description": description,
        }
        for node, description in zip(chosen, descriptions)
    ]


# -- actions ----------------------------------------------------------------


def tap(serial: str, x: int, y: int) -> None:
    shell(serial, f"input tap {int(x)} {int(y)}")


def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    shell(serial, f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")


def key(serial: str, keycode: int) -> None:
    shell(serial, f"input keyevent {int(keycode)}")


def scroll(serial: str, screen: tuple[int, int], direction: str) -> None:
    """Swipe to scroll. `scroll_down` means "reveal what is below", which is a
    swipe *up* — the inversion is the classic way to get this backwards."""
    width, height = screen
    cx = width // 2
    upper, lower = int(height * 0.35), int(height * 0.65)
    distance = min(SCROLL_PIXELS, int(height * 0.35))
    if direction == "scroll_down":
        swipe(serial, cx, lower, cx, max(upper, lower - distance))
    else:
        swipe(serial, cx, upper, cx, min(lower, upper + distance))


def scroll_region(serial: str, bounds: tuple[int, int, int, int], direction: str) -> None:
    """Scroll a verified container without swiping over neighboring controls."""
    left, top, right, bottom = bounds
    if right <= left or bottom <= top or direction not in ("scroll_down", "scroll_up"):
        raise ValueError("invalid scroll region or direction")
    cx = (left + right) // 2
    upper = top + int((bottom - top) * .2)
    lower = bottom - int((bottom - top) * .2)
    if direction == "scroll_down":
        swipe(serial, cx, lower, cx, upper)
    else:
        swipe(serial, cx, upper, cx, lower)


def is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def escape_text(text: str) -> str:
    """Prepare a string for `adb shell input text`.

    Two separate layers have to be satisfied, which is why this is fiddlier than
    it looks:

    * the **device shell** receives the command as one string, so the value is
      single-quoted and any embedded single quote is closed-escaped-reopened;
    * **`input text` itself** treats a space as an argument separator, so a space
      is sent as the `%s` escape it understands.

    Non-ASCII is refused by the caller rather than mangled here: `input text`
    cannot deliver it, and silently typing a corrupted value into a field is
    worse than declining.
    """
    return text.replace(" ", "%s")


def shell_quote(text: str) -> str:
    """Single-quote one argument for the device shell."""
    return "'" + text.replace("'", "'\\''") + "'"


def type_into(serial: str, text: str) -> None:
    if not is_ascii(text):
        raise AdbError(
            "`input text` cannot send non-ASCII characters, so this value was not typed.\n"
            "  install the ADBKeyboard IME to type Unicode, or retype the goal in ASCII."
        )
    shell(serial, f"input text {shell_quote(escape_text(text))}")


def open_url(serial: str, url: str) -> None:
    """Hand a URL to the system, which opens it in the browser the user defaulted.

    An Intent rather than driving the address bar, for three reasons: it needs no
    text entry (so it works with no text model configured, where `type_text` is
    not even offered), it works from any screen instead of requiring the browser
    to already be foreground, and it is one adb call rather than four taps.

    The URL is quoted for the device shell. It is already known to be a URL —
    `url_in` produced it — but `&` in a query string would still be interpreted
    as a command separator if it were not.
    """
    shell(serial, f"am start -a android.intent.action.VIEW -d {shell_quote(url)}")


# -- Facebook account location ----------------------------------------------


#: Facebook's in-app webview route. An `https://facebook.com/...` intent is not a
#: way in: the app publishes no App Link for it, so Android hands it to the
#: browser, naming the package is refused (`unable to resolve Intent`), and
#: starting the webview activity directly is blocked by
#: `com.facebook.permission.prod.FB_APP_COMMUNICATION`. This scheme is the one
#: door that opens the app's *own* webview, which is what makes the page readable
#: in the app rather than in Chrome.
FACEBOOK_APP = "com.facebook.katana"
FACEBOOK_SCHEME = "fb"
FACEBOOK_WEBVIEW_ROUTE = "facewebmodal/f?href="

#: The page that names the location Facebook currently attributes to the account
#: signed in. The `ref` is not decoration — without one the route renders an empty
#: shell.
FACEBOOK_PRIMARY_LOCATION_URL = (
    "https://www.facebook.com/primary_location/info?ref=bookmarks"
)


def restart_facebook(serial: str, *, deadline: float | None = None) -> None:
    """Force-stop and relaunch Facebook without clearing its saved app data."""
    def budget(limit: float) -> float:
        remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
        if remaining <= 0:
            raise AdbError("Facebook restart deadline expired")
        return remaining

    resolved = shell(
        serial,
        "cmd package resolve-activity --brief -a android.intent.action.MAIN "
        f"-c android.intent.category.LAUNCHER {FACEBOOK_APP}",
        timeout=budget(10),
    ).strip().splitlines()
    component = next((line.strip() for line in reversed(resolved)
                      if re.fullmatch(re.escape(FACEBOOK_APP) + r"/[A-Za-z0-9_.$]+", line.strip())), None)
    if not component:
        raise AdbError("Facebook launcher activity could not be resolved")
    shell(serial, f"am force-stop {FACEBOOK_APP}", timeout=budget(10))
    launched = shell(serial, f"am start -W -n {component}", timeout=budget(20))
    if re.search(r"(?:Error:|Exception|unable to resolve)", launched, re.IGNORECASE):
        raise AdbError("Facebook relaunch failed: " + launched.strip())

#: The webview paints slower than a native screen and offers no ready signal for
#: it, so the hierarchy is polled until the page names a location.
LOCATION_LOAD_TIMEOUT = 30.0
LOCATION_POLL_INTERVAL = 1.5

#: The label the page prints above the value, matched case-insensitively.
LOCATION_MARKER = "primary location is near"

#: A signed-out webview renders Facebook's sign-in page instead of the location.
#: Checked so the poll stops on it rather than waiting out the whole timeout.
_LOGIN_MARKERS = ("log into facebook", "forgotten password", "create new account")


def webview_link(url: str) -> str:
    """A `fb://` deep link that opens `url` in the Facebook app's own webview.

    The href is percent-encoded in full: the page URL's own `?` and `&` would
    otherwise be read as part of the deep link rather than as part of the page,
    and the route would open with a truncated query.
    """
    return f"{FACEBOOK_SCHEME}://{FACEBOOK_WEBVIEW_ROUTE}{quote(url, safe='')}"


def cache_busted(url: str, *, stamp: int | float | None = None) -> str:
    """Make the URL unique, so a re-run cannot be answered from the webview cache.

    Measured need rather than a precaution: the in-app webview keeps the last
    page, so a second check after switching accounts can render the *previous*
    account's answer. A changing query parameter forces a fresh fetch, which is
    what makes the result follow the session rather than the cache.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_jev={int(stamp if stamp is not None else time.time_ns())}"


def open_facebook_page(serial: str, url: str, *, timeout: float = ADB_TIMEOUT) -> None:
    """Open a page in the Facebook app's webview through its own deep link.

    `am start` reports a route it could not resolve on stdout with an `Error:`
    prefix and still exits 0, so the output is inspected rather than trusted.
    """
    link = webview_link(url)
    out = shell(serial, f"am start -a android.intent.action.VIEW -d {shell_quote(link)}",
                timeout=max(.1, timeout))
    if "Error:" in out:
        detail = out.strip().splitlines()[-1][:300]
        raise AdbError(
            f"the Facebook app did not open the page: {detail}\n"
            "  is the Facebook app installed on this phone, and this the right device?"
        )


def screen_text(serial: str) -> str:
    """The current screen's text, one line per node, from the view hierarchy.

    Deliberately not `snapshot()`: this needs no candidates, no screen size and no
    foreground app, so it skips three adb calls a poll would otherwise pay for.
    """
    return "\n".join(node.label() for node in parse_nodes(dump(serial)) if node.label())


def parse_primary_location(text: str) -> str | None:
    """The location out of the page's text, or `None` if the page is not showing it.

    The page prints the label and the value on separate lines — "Your primary
    location is near:" then "Lahore, Punjab 54" — so the value is the next
    non-empty line rather than something a single pattern can capture.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if LOCATION_MARKER not in line.lower():
            continue
        remainder = line.partition(":")[2].strip()
        if remainder:
            return remainder
        for following in lines[index + 1 :]:
            if following:
                # A partially rendered page may show only the explanation or
                # navigation below the heading. Those are not location values.
                if following.casefold().startswith(("primary location is determined", "learn more", "loading")) or following.casefold() in ("back", "your primary location"):
                    return None
                return following
    return None


def is_signed_out(text: str) -> bool:
    """Whether the webview landed on Facebook's sign-in page instead of the account."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _LOGIN_MARKERS)


def location_screen_state(text: str) -> str | None:
    """Recognize blockers from the screen, never from VPN or account geography."""
    lowered = text.casefold()
    if "session expired" in lowered and "please log in again" in lowered:
        return "session_expired"
    if "use fingerprint to unlock" in lowered or "swipe to unlock" in lowered or "emergency call" in lowered and "unlock" in lowered:
        return "locked"
    if "connection lost" in lowered or "webpage not available" in lowered or "network cannot access the internet" in lowered:
        return "network_error"
    if is_signed_out(text):
        return "login"
    return None


@dataclass
class AccountLocation:
    """What one check of the signed-in account's location page found.

    `state` is `"location"`, `"login"`, `"session_expired"`, `"locked"`, `"network_error"` or `"unknown"` — the last meaning the page
    neither named a location nor showed a sign-in form, which on a live phone
    means it had not finished rendering.
    """

    serial: str
    url: str
    location: str = ""
    state: str = "unknown"
    seconds: float = 0.0
    text: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    retries: int = 0

    def describe(self) -> str:
        lines = [
            f"device={self.serial}",
            f"app={FACEBOOK_APP}",
            f"state={self.state}",
        ]
        if self.location:
            lines.append(f"location={self.location}")
        elif self.state == "login":
            lines.append("location=(none — the app is showing a sign-in page)")
        elif self.state == "locked":
            lines.append("location=(none — unlock the phone and retry)")
        elif self.state == "network_error":
            lines.append("location=(none — restore the phone's internet connection and retry)")
        elif self.state == "session_expired":
            lines.append("location=(none — Facebook's session expired; sign in again on the phone and retry)")
        else:
            lines.append("location=(none found — the page had not rendered; see 'shows' below)")
        lines.append(f"seconds={self.seconds:.2f}")
        lines.append(f"url={self.url}")
        lines.append(
            "note=this answers for whichever account is signed in right now; "
            "switch accounts in the app and check again for the other one"
        )
        return "\n".join(lines)


def account_location(
    serial: str,
    *,
    url: str = FACEBOOK_PRIMARY_LOCATION_URL,
    timeout: float = LOCATION_LOAD_TIMEOUT,
    poll: float = LOCATION_POLL_INTERVAL,
) -> AccountLocation:
    """What location Facebook attributes to the account signed in on this phone.

    Opens the primary-location page in the Facebook app's own webview and polls
    the hierarchy until it names a location or settles on a sign-in page. The URL
    is cache-busted, so a check after switching accounts cannot be answered from
    the webview's copy of the previous account's page.

    This reads Facebook's own inference; it does not change it. The value is
    derived from the profile's current city, the connection's IP, check-ins and
    the device location — so accounts sharing a phone and a network can still
    agree unless their profile cities or activity differ.
    """
    started = time.monotonic()
    target = cache_busted(url)
    timings = {"open": 0.0, "hierarchy_reads": 0.0, "recovery": 0.0}
    deadline = started + max(0.0, timeout)
    if time.monotonic() >= deadline:
        return AccountLocation(serial, target, state="unknown", seconds=0.0,
                               timings={"open": 0.0, "hierarchy_reads": 0.0, "recovery": 0.0})
    open_started = time.monotonic()
    open_facebook_page(serial, target, timeout=max(.1, deadline - time.monotonic()))
    timings["open"] += time.monotonic() - open_started

    text = ""
    retries = 0
    recovery_checked = False
    recovery_at = started + min(4.0, max(1.0, timeout * .2))
    while True:
        read_started = time.monotonic()
        text = screen_text(serial)
        timings["hierarchy_reads"] += time.monotonic() - read_started
        location = parse_primary_location(text)
        if location or location_screen_state(text):
            break
        now = time.monotonic()
        # A blank page may be a missed webview navigation. Allow one bounded,
        # read-only re-open after a short observation window, within the same
        # per-account deadline. Never repeat account selection or attribution.
        if retries == 0 and not recovery_checked and now >= recovery_at and now < deadline:
            recovery_checked = True
            evidence = snapshot(serial)
            if time.monotonic() >= deadline:
                break
            webview_visible = any("WebView" in node.cls for node in evidence.nodes)
            page_context = "primary location" in evidence.read().casefold()
            if not evidence.foreground.startswith(FACEBOOK_APP + "/") or not (webview_visible or page_context):
                if now >= deadline:
                    break
                time.sleep(min(max(0.0, poll), max(0.0, deadline - time.monotonic())))
                continue
            recovery_started = time.monotonic()
            key(serial, KEYCODES["go_back"])
            if time.monotonic() >= deadline:
                break
            target = cache_busted(url)
            open_facebook_page(serial, target, timeout=max(.1, deadline - time.monotonic()))
            timings["recovery"] += time.monotonic() - recovery_started
            retries = 1
            continue
        if now >= deadline:
            break
        time.sleep(min(max(0.0, poll), max(0.0, deadline - now)))

    location = parse_primary_location(text) or ""
    state = "location" if location else location_screen_state(text) or "unknown"
    return AccountLocation(
        serial=serial,
        url=target,
        location=location,
        state=state,
        seconds=time.monotonic() - started,
        text=text,
        timings=timings,
        retries=retries,
    )


# -- observation ------------------------------------------------------------


class Observation:
    """One screen snapshot, shaped for the chooser.

    Quacks like the browser observation so `JevChooser`, `validate` and the loop
    need no Android branch: they read `target_map()`, `operations()`,
    `targets_for()` and `readouts()`.
    """

    def __init__(
        self,
        serial: str,
        nodes: list[Node],
        screen: tuple[int, int],
        foreground: str = "",
        can_write: bool = False,
        navigate_url: str | None = None,
    ) -> None:
        self.serial = serial
        self.nodes = nodes
        self.screen = screen
        self.foreground = foreground
        self.can_write = can_write
        self.navigate_url = navigate_url

        # The chooser's state carries `title` and `pid`; on Android the useful
        # equivalent of a title is the app, and there is no single pid to name.
        self.title = foreground
        self.pid = 0
        self.window_id = 0
        self.url = foreground

        self._candidates = build_candidates(nodes, screen)
        self._fields = [c for c in self._candidates if c["fillable"]]

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return self._candidates

    @property
    def targets(self) -> list[dict[str, Any]]:
        return [c for c in self._candidates if c["clickable"]]

    @property
    def fields(self) -> list[dict[str, Any]]:
        return self._fields

    def by_id(self, candidate_id: str) -> dict[str, Any] | None:
        return next((c for c in self._candidates if c["id"] == str(candidate_id)), None)

    def option_map(self) -> dict[str, str]:
        return {c["id"]: c["description"] for c in self._candidates}

    def target_map(self) -> dict[str, str]:
        return {c["id"]: c["description"] for c in self.targets}

    def operations(self) -> dict[str, str]:
        operations = dict(ANDROID_OPERATIONS)
        if not self.targets:
            operations.pop("click_element", None)
        # Typing needs somewhere to type AND a writer to produce the string; Jev
        # cannot generate text, so offering it without a writer is a trap.
        if not self._fields or not self.can_write:
            operations.pop("type_text", None)
        # Only when the goal names a URL, so it is never a free choice.
        if self.navigate_url:
            operations.update(NAVIGATE_OPERATION)
        return operations

    def target_heads(self) -> dict[str, dict[str, str]]:
        heads = {"click_target": self.target_map()}
        if "type_text" in self.operations():
            heads["text_target"] = {c["id"]: c["description"] for c in self._fields}
        return heads

    def targets_for(self, operation: str) -> list[dict[str, Any]]:
        if operation == "click_element":
            return self.targets
        if operation == "type_text":
            return self._fields
        return []

    def readouts(self, limit: int = 30) -> list[str]:
        """What the screen says, which is how a one-action-at-a-time model learns
        that its last action landed.

        Password fields contribute their presence but never their contents.
        """
        shown: list[str] = []
        if self.foreground:
            shown.append(f"app: {self.foreground}")
        for node in self.nodes:
            if node.password:
                if node.label() not in shown:
                    shown.append("[password field]")
                continue
            text = node.label()
            if text and text not in shown:
                shown.append(text)
            if len(shown) >= limit:
                break
        return shown

    def read(self) -> str:
        """The screen's text, one line per node, for `browser_read`-style answering."""
        lines: list[str] = []
        for node in self.nodes:
            if node.password:
                lines.append("[password field]")
                continue
            text = node.label()
            if text:
                lines.append(text)
        return "\n".join(lines)


# -- the loop ---------------------------------------------------------------


@dataclass
class Step:
    index: int
    decision: Decision
    observation: Observation
    executed: bool
    note: str = ""


@dataclass
class Result:
    goal: str
    steps: list[Step] = field(default_factory=list)
    outcome: str = "max_steps"
    seconds: float = 0.0
    snapshots: int = 0
    foreground: str = ""
    subgoals: list[str] = field(default_factory=list)

    @property
    def actions(self) -> int:
        return sum(1 for s in self.steps if s.executed)

    @property
    def url(self) -> str:
        """Where we are, named the way the browser engine names it.

        The shared renderer reports the current location for either engine, and
        on Android the useful location is the foreground app, not a URL.
        """
        return self.foreground


def snapshot(serial: str, goal: str = "", *, can_write: bool = False) -> Observation:
    xml_text = dump(serial)
    return Observation(
        serial=serial,
        nodes=parse_nodes(xml_text),
        # Cached against the rotation reported by this very dump, so it is one
        # `wm size` per orientation change instead of one per step.
        screen=cached_screen_size(serial, rotation_of(xml_text)),
        foreground=foreground(serial),
        can_write=can_write,
        navigate_url=url_in(goal),
    )


def execute(serial: str, decision: Decision, observation: Observation) -> None:
    if decision.kind == "click_element":
        found = observation.by_id(decision.element_id or "")
        if found is None:
            raise AdbError(f"element {decision.element_id!r} is not on the current screen")
        x, y = found["centred"]
        tap(serial, x, y)
        return
    if decision.kind == "navigate":
        if not observation.navigate_url:
            raise AdbError("navigate was chosen but the goal names no URL")
        open_url(serial, observation.navigate_url)
        return
    if decision.kind in ("scroll_down", "scroll_up"):
        scroll(serial, observation.screen, decision.kind)
        return
    if decision.kind in KEYCODES:
        key(serial, KEYCODES[decision.kind])
        return
    if decision.kind == "wait":
        time.sleep(1.0)
        return
    if decision.kind == "type_text":
        raise ValueError("type_text needs the text resolved first; use run()")
    raise ValueError(f"no android executor for {decision.kind!r}")


def _run_one(
    serial: str,
    goal: str,
    chooser: Any,
    *,
    act: bool,
    max_steps: int,
    min_confidence: float,
    settle: float,
    writer: Any = None,
    observation: Observation | None = None,
) -> Result:
    """One goal, one Jev loop. No planning here — see run().

    `observation` seeds the first step: `run()` may already hold the dump it read
    to plan with, and on Android re-taking it is the single most expensive mistake
    available — a hierarchy dump is ~2.1 s. It is consumed once; every later step
    re-observes.
    """
    started = time.perf_counter()
    result = Result(goal=goal)
    history: list[str] = []
    can_write = writer is not None and writer.available
    pending = observation

    for index in range(max_steps):
        if pending is not None:
            observation = pending
            pending = None
        else:
            observation = snapshot(serial, goal, can_write=can_write)
        result.snapshots += 1
        result.foreground = observation.foreground

        # First step, unambiguous goal: decide it in code rather than paying for a
        # model round trip. Later steps always go to the chooser.
        fixed = deterministic_decision(goal, observation) if index == 0 else None
        chosen = fixed if fixed is not None else chooser.choose(goal, observation, history)
        decision = validate(chosen, observation)
        if not decision.accepted:
            result.steps.append(
                Step(index, decision, observation, False, f"refused: {decision.rejection}")
            )
            result.outcome = "refused"
            break
        if decision.kind in ("done", "impossible"):
            result.steps.append(Step(index, decision, observation, False, decision.kind))
            result.outcome = decision.kind
            break
        if decision.confidence < min_confidence:
            note = f"low confidence {decision.confidence:.2f} < {min_confidence:.2f}"
            result.steps.append(Step(index, decision, observation, False, note))
            result.outcome = "low_confidence"
            break

        label = (observation.by_id(decision.element_id or "") or {}).get("description", "")

        text: str | None = None
        if decision.kind == "type_text":
            if writer is None:
                result.steps.append(
                    Step(index, decision, observation, False,
                         f"refused: type_text needs a text model (field {label})")
                )
                result.outcome = "refused"
                break
            text = writer.write(goal, label, observation.title)
            if text is None:
                result.steps.append(
                    Step(index, decision, observation, False,
                         f"refused: the writer declined to fill {label}")
                )
                result.outcome = "refused"
                break
            if not is_ascii(text):
                result.steps.append(
                    Step(index, decision, observation, False,
                         "refused: `input text` cannot send non-ASCII")
                )
                result.outcome = "refused"
                break
            label = f"{label} <- {text!r}"

        note = f"{decision.kind} {label}".strip()
        if act:
            # Only a state-changing action is settled; type/scroll/wait are not, so the
            # baseline is captured only when the poll will actually use it.
            settles = decision.kind in ("click_element", "go_back", "go_home", "navigate")
            before = focus_signature(serial) if settles else ""
            if decision.kind == "type_text" and text is not None:
                type_into(serial, text)
            else:
                execute(serial, decision, observation)
            if settles:
                # Poll a ~50 ms signal, never a full snapshot. This loop only ever
                # asked one question — "has the screen changed?" — and answering it
                # with `snapshot()` cost ~2.9 s a go, most of a step's budget spent
                # re-serialising a whole hierarchy to read one string off it. The
                # window signature is that same cheap `dumpsys window`, and it moves
                # for a dialog or a new surface that the foreground app alone misses.
                # An unreadable device (`""`) is not a move — see `_screen_moved`.
                deadline = time.perf_counter() + settle
                while time.perf_counter() < deadline:
                    if _screen_moved(before, focus_signature(serial)):
                        break
                    time.sleep(0.25)
            note = f"acted: {note}"
        else:
            note = f"would: {note}"

        result.steps.append(Step(index, decision, observation, act, note))
        history.append(note)

    result.seconds = time.perf_counter() - started
    return result


def run(
    serial: str,
    goal: str,
    chooser: Any,
    *,
    act: bool = False,
    max_steps: int = 8,
    min_confidence: float = 0.4,
    settle: float = DEFAULT_SETTLE,
    writer: Any = None,
    decompose: bool = True,
    cache: Any = None,
) -> Result:
    """Drive the phone toward a goal, optionally planning first.

    Same contract as the browser engine: Jev decides one action at a time from
    the current screen and cannot hold a plan, so a compound goal is split into
    ordered subgoals before the loop starts.
    """
    started = time.perf_counter()

    if cache is not None:
        # Namespaced, because the browser engine shares this cache file and its keys
        # start with a URL. A package/activity name must never be able to collide
        # with one.
        #
        # The key carries the window signature, not just the foreground app, at the
        # same single `dumpsys window` cost (~52 ms): a dialog or a new surface
        # changes the focused window where the app string does not, so this avoids
        # some — not all — false lookups. An ordinary tab or fragment inside one
        # activity keeps the same window and is NOT distinguished (see
        # focus_signature); those are caught at replay time by description
        # resolution. Computed only when caching is on, so a plain run pays nothing.
        signature = focus_signature(serial)
        if not signature:
            # The screen cannot be identified, so there is no safe key. Sharing one
            # "unknown" key across every unreadable screen would replay the wrong
            # plan, so caching is skipped for this run entirely.
            cache = None
        else:
            start_key = f"android|{signature}"
            stored = cache.get(f"{start_key}|{goal}", goal)
            if stored:
                replayed = replay(serial, goal, stored, act=act, settle=settle, writer=writer)
                if replayed.outcome == "replayed":
                    return replayed
                cache.drop(f"{start_key}|{goal}", goal)

    if writer is None or not writer.available or not decompose:
        result = _run_one(
            serial, goal, chooser, act=act, max_steps=max_steps,
            min_confidence=min_confidence, settle=settle, writer=writer,
        )
        if cache is not None and act and result.outcome == "done":
            cache.put(f"{start_key}|{goal}", goal, plan_from(result))
        return result

    can_write = writer is not None and writer.available
    # The planner needs the screen's text, and reading it means a full dump. That
    # dump is also exactly the observation the loop starts from, so it is handed
    # over rather than re-taken — on Android a second dump is ~2.1 s.
    summary_observation: Observation | None = None
    try:
        summary_observation = snapshot(serial, goal, can_write=can_write)
        summary = summary_observation.read()
    except AdbError:
        summary = ""
    subgoals = writer.decompose(goal, summary)

    if len(subgoals) < 2:
        return _run_one(
            serial, goal, chooser, act=act, max_steps=max_steps,
            min_confidence=min_confidence, settle=settle, writer=writer,
            observation=summary_observation,
        )

    combined = Result(goal=goal)
    remaining = max_steps
    pending = summary_observation
    for subgoal in subgoals:
        if remaining <= 0:
            break
        seed = None
        if pending is not None:
            # Reuse the summary dump for the first subgoal only, correcting the URL
            # it was read for: `navigate` is offered only when the current goal
            # names one, and the subgoal is not the goal the dump was taken for.
            pending.navigate_url = url_in(subgoal)
            seed, pending = pending, None
        part = _run_one(
            serial, subgoal, chooser, act=act, max_steps=remaining,
            min_confidence=min_confidence, settle=settle, writer=writer,
            observation=seed,
        )
        combined.steps.extend(part.steps)
        combined.snapshots += part.snapshots
        combined.foreground = part.foreground
        remaining -= max(1, len(part.steps))
        combined.outcome = part.outcome
        if part.outcome != "done":
            break

    combined.seconds = time.perf_counter() - started
    combined.subgoals = subgoals
    if cache is not None and act and combined.outcome == "done":
        cache.put(f"{start_key}|{goal}", goal, plan_from(combined))
    return combined


def plan_from(result: Result) -> list[dict[str, str]]:
    """Reduce a successful run to a replayable plan.

    Stores the element's DESCRIPTION, never its ref: refs are positions in one
    dump and mean something different on the next screen, exactly like the
    browser engine's snapshot-scoped refs.
    """
    plan: list[dict[str, str]] = []
    for step in result.steps:
        if not step.executed:
            continue
        decision = step.decision
        if decision.kind in ("click_element", "type_text"):
            found = step.observation.by_id(decision.element_id or "")
            if found is None:
                return []
            plan.append({"kind": decision.kind, "target": found["description"]})
        elif decision.kind in ("scroll_down", "scroll_up", "go_back", "go_home", "navigate"):
            plan.append({"kind": decision.kind, "target": ""})
    return plan


def replay(
    serial: str,
    goal: str,
    plan: list[dict[str, str]],
    *,
    act: bool = False,
    settle: float = DEFAULT_SETTLE,
    writer: Any = None,
    observation: Observation | None = None,
) -> Result:
    """Execute a cached plan against one fresh screen. No model calls.

    A description that no longer resolves ABORTS rather than guessing: a stale
    plan is worse than no plan.
    """
    started = time.perf_counter()
    result = Result(goal=goal)
    can_write = writer is not None and writer.available

    if not plan or not plan[0].get("target"):
        # The FIRST executable action must be a described target. A targetless first
        # action (back/home/scroll/navigate) runs before anything has validated the
        # screen, so on a different screen inside the same activity it would act on
        # the wrong one — a plan like `go_back -> click "Settings"` would go back on
        # the wrong screen before "Settings" was ever resolved. A described first
        # target validates the screen, and every later action is resolved against a
        # re-dumped screen, so those are safe.
        result.outcome = "replay_miss"
        result.seconds = time.perf_counter() - started
        return result

    if observation is None:
        observation = snapshot(serial, goal, can_write=can_write)
    result.snapshots = 1
    result.foreground = observation.foreground

    for index, entry in enumerate(plan):
        kind = entry.get("kind", "")
        wanted = entry.get("target", "")

        if kind in ("scroll_down", "scroll_up", "go_back", "go_home", "navigate"):
            decision = Decision(kind=kind, confidence=1.0, source="cache")
        else:
            # Resolve only within the elements LEGAL for this operation, so a cached
            # click cannot land on a non-clickable candidate or typing on a non-fillable
            # one.
            legal = observation.targets_for(kind)
            matches = [c for c in legal if c["description"] == wanted]
            if len(matches) != 1:
                # Exactly one, or it is not replayable: zero is a target that is gone,
                # more than one is an ambiguous description, and silently taking the
                # first could act on the wrong control.
                reason = (
                    f"nothing described {wanted!r}"
                    if not matches
                    else f"{len(matches)} elements match {wanted!r}"
                )
                result.steps.append(
                    Step(index, Decision(kind=kind, source="cache"), observation, False,
                         f"replay_miss: {reason}")
                )
                result.outcome = "replay_miss"
                result.seconds = time.perf_counter() - started
                return result
            decision = Decision(
                kind=kind, element_id=matches[0]["id"], confidence=1.0, source="cache"
            )

        # A cached action passes the same gate a live one does.
        decision = validate(decision, observation)
        if not decision.accepted:
            result.steps.append(
                Step(index, decision, observation, False, f"replay_miss: {decision.rejection}")
            )
            result.outcome = "replay_miss"
            result.seconds = time.perf_counter() - started
            return result

        note = f"cache: {kind} {wanted or ''}".strip()
        if act:
            has_next = index < len(plan) - 1
            # A state-changing action is settled even when it is LAST, so a final tap
            # does not return `replayed` while the transition is still underway. Only
            # the ~2.1 s re-dump is skipped when nothing later reads it.
            settles = kind in ("click_element", "navigate", "go_back", "go_home")
            # Baseline BEFORE acting. Captured afterwards it compares the new screen
            # with itself, so the poll never fires and every replay waits out the full
            # settle even when the screen changed at once. `""` (unreadable) is not a
            # move, so `_screen_moved` governs the comparison.
            settled = focus_signature(serial) if settles else ""
            if kind == "type_text":
                if writer is None:
                    result.outcome = "replay_miss"
                    return result
                text = writer.write(goal, wanted, observation.title)
                if text is None or not is_ascii(text):
                    result.outcome = "replay_miss"
                    return result
                type_into(serial, text)
            else:
                execute(serial, decision, observation)
            # Settle on the cheap signal first, then take ONE snapshot and carry it
            # into the next step. Replaying used to dump the hierarchy immediately
            # after acting, which could catch a half-drawn transition and hand the
            # next step a screen that no longer existed. go_back/go_home are settled
            # too — they change the screen just as a tap does.
            if settles:
                deadline = time.perf_counter() + settle
                while time.perf_counter() < deadline:
                    current = focus_signature(serial)
                    if _screen_moved(settled, current):
                        # Keep the report honest without paying for a dump.
                        result.foreground = current.split("|", 1)[0] or result.foreground
                        break
                    time.sleep(0.25)
            # Only re-dump if a LATER entry still has to resolve against the screen.
            # A dump after the final action is ~2.1 s that nothing reads.
            if has_next:
                observation = snapshot(serial, goal, can_write=can_write)
                result.snapshots += 1
                result.foreground = observation.foreground
            note = f"cache: acted {kind} {wanted}".strip()

        result.steps.append(Step(index, decision, observation, act, note))

    result.outcome = "replayed"
    result.seconds = time.perf_counter() - started
    return result
