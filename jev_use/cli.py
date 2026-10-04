"""Command line entry point for jev-use."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cache import PlanCache
from .candidates import Observation, observe
from .choosers import MIN_CONFIDENCE_DEFAULT, JevChooser, MockChooser
from .driver import Driver, DriverError
from .loop import RunResult, plan_from_steps, replay_plan, run


def parse_script(text: str | None) -> list[dict[str, Any]] | None:
    """`click:0,click:1,press_key:return,done` -> script steps."""
    if not text:
        return None
    steps: list[dict[str, Any]] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            steps.append({"kind": chunk})
            continue
        kind, _, value = chunk.partition(":")
        kind, value = kind.strip(), value.strip()
        if kind == "click":
            steps.append({"kind": "click_element", "element_index": int(value)})
        elif kind == "press_key":
            steps.append({"kind": "press_key", "key": value})
        else:
            steps.append({"kind": kind, "element_index": int(value) if value.isdigit() else None})
    return steps


class FixtureDriver:
    """Replays a saved observation. Actions are never sent anywhere."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append(tool)
        if tool == "get_window_state":
            return _FakeResult(self.payload)
        raise DriverError(f"fixture mode cannot execute {tool!r}")


class _FakeResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def resolve_window(driver: Driver, title: str | None) -> tuple[int, int, str]:
    result = driver.call("list_windows", {"on_screen_only": False})
    windows = result.json() or {}
    records = windows.get("windows", windows if isinstance(windows, list) else [])
    if not records:
        raise DriverError(
            "The driver reports no windows.\n"
            "On GNOME/Wayland this means the WinRects helper is not loaded yet.\n"
            "  gnome-extensions info winrects@cua   # want: State: ACTIVE\n"
            "If it is not ACTIVE, log out and back in once, then retry."
        )

    if title:
        matches = [w for w in records if title.lower() in (w.get("title") or "").lower()]
        if not matches:
            raise DriverError(f"no window title contains {title!r}")
        chosen = matches[0]
    else:
        chosen = records[0]

    return (
        int(chosen["pid"]),
        int(chosen["window_id"]),
        chosen.get("title") or chosen.get("app_name") or "",
    )


def window_title_for(driver: Driver, pid: int, window_id: int) -> str:
    """The cache key needs a stable window identifier; the title is the best one."""
    try:
        payload = driver.call("list_windows", {"on_screen_only": False}).json() or {}
    except DriverError:
        return ""
    for record in payload.get("windows", []):
        if record.get("window_id") == window_id:
            return record.get("title") or record.get("app_name") or ""
    return ""


def report(result: RunResult, act: bool, cache_note: str | None = None) -> None:
    mode = "ACT" if act else "DRY RUN"
    print(f"\n{mode} — goal: {result.goal}")
    print("-" * 68)
    for step in result.steps:
        marker = ">" if step.executed else " "
        print(f"{marker} [{step.index}] {step.note}")

    source = result.steps[0].decision.source if result.steps else "n/a"
    actions = result.actions
    per_action = result.seconds / actions if actions else 0.0
    print("-" * 68)
    print(
        f"outcome: {result.outcome}   actions: {actions}   steps: {len(result.steps)}   "
        f"snapshots: {result.observations}"
    )
    print(
        f"time: {result.seconds:.2f}s   per action: {per_action * 1000:.0f} ms   "
        f"per snapshot: {result.seconds / max(1, result.observations) * 1000:.0f} ms   "
        f"chooser: {source}"
    )
    if cache_note:
        print(f"cache: {cache_note}")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "call":
        # The Python console script predates the npm dispatcher. Keep the
        # documented direct-tool subcommand available when that console script
        # is the executable found on PATH.
        from .tool_cli import main as tool_main

        return tool_main(arguments[1:])

    parser = argparse.ArgumentParser(
        prog="jev-use",
        description="Drive one window toward a goal with Cua Driver + a typed chooser.",
    )
    parser.add_argument("goal", nargs="?", help="what to accomplish, in plain English")
    parser.add_argument("--live", action="store_true", help="use TypeSafe Jev (needs TYPESAFE_API_KEY)")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "TypeSafe model id. Defaults to the pinned "
            f"{JevChooser.DEFAULT_MODEL}; pass jev-latest only if you accept "
            "answers shifting under you."
        ),
    )
    parser.add_argument("--act", action="store_true", help="actually send input (default: dry run)")
    parser.add_argument("--pid", type=int, help="target process id")
    parser.add_argument("--window-id", type=int, help="target window id")
    parser.add_argument("--title", help="pick the first window whose title contains this")
    parser.add_argument("--fixture", help="replay a saved observation instead of the desktop")
    parser.add_argument("--dump-fixture", help="save the first observation to this path and exit")
    parser.add_argument("--script", help="mock script, e.g. 'click:0,click:1,press_key:return,done'")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE_DEFAULT)
    parser.add_argument(
        "--actions-per-snapshot",
        type=int,
        default=None,
        help=(
            "Actions to take from one accessibility snapshot. Defaults to 1 with "
            "--live (a batched snapshot is stale for every action after the first, "
            "and a one-action-at-a-time model cannot see its own effect) and 5 "
            "otherwise, where the sequence is already known good."
        ),
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=0.0,
        help="extra seconds to wait after acting (default 0: re-observation is the check)",
    )
    parser.add_argument("--no-cache", action="store_true", help="do not read or write the plan cache")
    parser.add_argument("--cache-path", default=".jev-cache.json")
    parser.add_argument("--forget-cache", action="store_true", help="drop this goal's cached plan")
    parser.add_argument("--list-windows", action="store_true", help="print visible windows and exit")
    parser.add_argument("--list-tools", action="store_true", help="print driver tools and exit")

    args = parser.parse_args(arguments)

    if args.list_tools:
        with Driver() as driver:
            for name in driver.list_tools():
                print(name)
        return 0

    if args.list_windows:
        with Driver() as driver:
            result = driver.call("list_windows", {"on_screen_only": False})
            payload = result.json() or {}
            records = payload.get("windows", [])
            if not records:
                print("no windows reported (on GNOME/Wayland, load the WinRects helper first)")
                return 1
            for record in records:
                print(f'{record["window_id"]:>10}  pid {record["pid"]:<7} {record.get("title", "")}')
        return 0

    if not args.goal:
        parser.error("a goal is required (or pass --list-windows / --list-tools)")

    script = parse_script(args.script)
    chooser = (
        JevChooser(model=args.model) if args.live else MockChooser(script=script)
    )

    if args.fixture and args.act:
        parser.error("--fixture replays a saved observation and cannot --act")

    if args.fixture:
        payload = json.loads(Path(args.fixture).read_text())
        driver: Any = FixtureDriver(payload)
        observation = Observation.from_payload(payload)
        pid, window_id = observation.pid, observation.window_id
    else:
        driver = Driver()
        driver.start()
        try:
            if args.pid and args.window_id:
                pid, window_id = args.pid, args.window_id
            else:
                pid, window_id, resolved = resolve_window(driver, args.title)
                print(f"target: pid {pid} window {window_id} {resolved!r}")
        except DriverError as exc:
            print(f"error: {exc}", file=sys.stderr)
            driver.close()
            return 2

    cache = None if args.no_cache else PlanCache(args.cache_path)
    cache_note: str | None = None

    try:
        if args.dump_fixture:
            observation = observe(driver, pid, window_id)
            Path(args.dump_fixture).write_text(json.dumps(observation.to_payload(), indent=2))
            print(
                f"wrote {args.dump_fixture} "
                f"({len(observation.elements)} elements, {len(observation.candidates)} candidates)"
            )
            return 0

        acting = args.act and not args.fixture
        window_title = (
            observation.title
            if args.fixture
            else window_title_for(driver, pid, window_id)
        )

        if cache and args.forget_cache:
            cache.drop(window_title, args.goal)

        plan = cache.get(window_title, args.goal) if cache else None
        result = None

        if plan:
            observation = observe(driver, pid, window_id)
            result = replay_plan(
                driver, plan, observation, act=acting, settle_seconds=args.settle
            )
            result.goal = args.goal
            if result.outcome == "replayed":
                cache_note = f"HIT — replayed {len(plan)} actions, zero model calls"
            else:
                cache_note = f"stale entry ({result.outcome}) — re-planning"
                cache.drop(window_title, args.goal)
                result = None

        if result is None:
            # A live chooser decides one action at a time and needs to see the
            # effect of each one, so a stale batched snapshot would make it walk
            # past the correct next step.
            per_snapshot = args.actions_per_snapshot
            if per_snapshot is None:
                per_snapshot = 1 if args.live else 5

            result = run(
                driver,
                args.goal,
                pid,
                window_id,
                chooser,
                act=acting,
                max_steps=args.max_steps,
                min_confidence=args.min_confidence,
                actions_per_snapshot=per_snapshot,
                settle_seconds=args.settle,
            )
            if cache:
                if result.outcome == "done" and acting:
                    new_plan = plan_from_steps(result.steps)
                    cache.put(window_title, args.goal, new_plan)
                    cache_note = f"stored {len(new_plan)} actions for next run"
                else:
                    cache_note = "MISS (only successful, acted runs are cached)"
    except DriverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if isinstance(driver, Driver):
            driver.close()

    report(result, act=args.act, cache_note=cache_note)
    return 0 if result.outcome in ("done", "replayed") or not args.act else 1


if __name__ == "__main__":
    raise SystemExit(main())
