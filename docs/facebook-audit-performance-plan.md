# Facebook account audit performance plan

## Evidence

Reviewed CommandCode session `cceffb70-e808-4c66-a3bc-fa72f8fc4fe2`
(`Facebook Location Audit`) and the current Android workflow source.
Session storage: `~/.commandcode/projects/home-hamza-hamza-no-work/`.

- Session timestamps span 2026-10-03 20:20:04.436–21:03:24.594 UTC:
  43 minutes 20 seconds. From the first persisted user message: 43 minutes 9 seconds.
- 97 assistant turns; median interval between assistant records: 23.5 seconds.
- 41 `android_use`, 34 `android_read`, 8 `android_location`, 7
  `android_facebook`, 1 device listing, 2 tool searches, 2 sleeps, 2 todo writes.
- Explicit durations reported by action/location/Facebook tools total 469.6
  seconds. Sleeps add 40 seconds. Screen-read time is not reported.
- About 34 minutes 50 seconds remains unattributed between screen reads,
  model generation, provider/runtime overhead, and other unreported work.
  Persisted tool-call and result timestamps are effectively identical, so they
  cannot establish individual tool start times. Do not label this entire
  remainder as model inference time.
- Three blank location-page attempts consumed 46.24, 46.32, and 60.91 seconds.
  Each retry returned a location in roughly 6 seconds.
- A 194-second interval before one screen read has no explanatory timing data.
- Model: Qwen/Qwen3.8-Omni-Flash. Reported input per response grew from about
  21k to as much as 66k tokens; cumulative reported input was 4.43 million,
  including repeated/cached context. This is not 4.43 million unique tokens.
- Only five distinct accounts had location results; no final completion report
  is present in the saved transcript.

## Root causes

1. `Workflow.accounts()` rejects any scrollable hierarchy. `select()` calls
   `accounts()`, so this completeness check also prevents selecting an exact
   visible account. The agent abandoned the deterministic workflow.
2. Enumeration leaves an account sheet open. `menu()` tries one Dismiss tap,
   but the transcript shows that tap did not close the sheet. Repeated reads,
   taps, and retries followed before the first successful location result.
3. Manual fallback required a model round trip for nearly every tap/read.
   One account switch became many orchestration steps instead of one operation.
4. Navigation recovery was not state driven: repeated Back presses, attempts
   to use unavailable switcher controls, and Menu guesses that opened
   Notifications. The existing native-tab recognizer supports five/six tabs;
   the final observed layout was reported as four tabs. Unsupported layouts
   should return a bounded failure, not trigger repeated guesses.
5. Blank webviews incurred full timeouts. The repeated first-failure/second-
   success pattern needs a device trace; the transcript cannot establish why
   the initial page failed to render.
6. The agent rechecked Tahir's location because profile city differed from
   primary location. These are different fields; the discrepancy alone does
   not prove stale data. Identity verification still must remain mandatory.

## Implementation order

### 1. Instrument before changing timeout values

Add monotonic durations for device discovery, hierarchy reads, screenshots,
switcher navigation, selection, login wait, location opening/polling, and
identity verification. Record tool receipt/start/finish, retries, outcome,
and model round-trip timing where the harness exposes it. Keep secrets and
full account content out of performance logs. Return aggregate timings.

Acceptance: a run's wall time can be reconciled against measured phases;
unmeasured provider latency remains explicitly unknown.

### 2. Fix enumeration and exact selection

In `jev_use/facebook_android.py`, enumerate the account picker with bounded,
scoped scrolling. Deduplicate exact observed names, confirm the end of the
list, detect no-progress and ambiguous duplicate names, and restore Menu.
Separate complete enumeration from selecting a requested observed account.
Locate a target while scrolling and select it once; do not replay a timed-out
selection. Preserve identity verification before/after every location read.

Acceptance: all ten accounts are enumerated and each is reachable without
`android_use`; a partial list is never labeled complete.

### 3. Implement bounded state-based navigation

Recognize Menu, profile dropdown, saved-account sheet, feed, location webview,
login transition, authentication prompt, and unsupported layout. Use current
observations for actions and verify the resulting state. Prefer explicit
labels; add layout variants only from observed evidence. Do not equate the
last tab with Menu on a four-tab bar. Bound recovery and stop repeated
no-progress actions.

Acceptance: no ineffective Dismiss loop, no blind Back chain to the launcher,
no repeated Notifications taps, and a useful unsupported-layout result.

### 4. Diagnose and repair location-page readiness

Measure page opening and hierarchy changes on the actual device. Determine
whether nested webviews, login readiness, routing, or hierarchy availability
cause the blank first attempt. Keep fresh URLs and session verification.
Use condition-based waits. After a measured short blank-page interval, allow
at most one read-only page recovery; enforce a total per-account deadline.
Do not retry selection or misreport a timed-out account as checked.

Acceptance: the three observed blank-page cases are covered by captured
fixtures or a reproducible device scenario; healthy reads avoid full 45–60s
waits, while slow legitimate loads remain distinguishable from failures.

### 5. Add an audit operation and resumable progress

Extend the Facebook tool with `action=audit` in the workflow and MCP schema
(`jev_use/mcp_server.py`), plus CLI support where needed. One call enumerates
and sequentially processes accounts. Stream per-account progress if supported;
otherwise use bounded chunks plus a resume token. Coordinate with MCP request
timeouts. Persist verified results as each account finishes and reuse them
on resume. Never run conflicting phone actions concurrently.

Return structured results with account, location, identity_verified, state,
elapsed time, and failure detail. Stop on device/authentication blockers;
continue independent accounts only when the observed state permits it.

Acceptance: one normal audit call (or a small number of timeout-safe chunks),
no per-tap model round trips, no lost verified results on interruption.

### 6. Update the mobile-use skill and benchmark

Update the packaged skill and installed copy used by CommandCode to prefer
audit and prohibit manual guessed-tap recovery after unsupported layouts.
Reload its MCP process so the new schema is live. Validate argument types,
including real booleans/numbers rather than repaired strings.

Add targeted tests in `tests/test_facebook_android.py` and `tests/test_android.py`
for scrollable enumeration, offscreen selection, duplicate names, no progress,
ineffective dismissal, four-tab layouts, identity mismatch, blank-page recovery,
and interruption/resume. Verify MCP/CLI schemas and skill consistency.

Run a live ten-account benchmark when the phone is available. Report total
time, time per account, tool/model calls, retries, and verified completion
count. Initial engineering target: at least 5x faster than this 43-minute
baseline with all ten accounts processed. This is a target, not a measured
promise; revise it using device timing. Do not trade identity correctness for
speed or claim success when accounts remain unchecked.

## Implementation status (2026-10-04)

Three GPT-6 Luna agents implemented the workflow, resumable audit tool, and
skill/benchmark updates. Parent review added a deadline regression guard.
Implemented components:

- Scoped picker enumeration/selection with terminal evidence and ambiguity checks.
- Bounded observed-state recovery, shared account budget, and the visually
  confirmed top six-tab layout in addition to existing bottom variants.
- Evidence-gated one-time blank-webview recovery, fresh URLs, identity checks,
  phase timings, and explicit unknown provider timing.
- `android_facebook(action="audit")`, default two-account chunks with a 52s
  call budget and 50s account budget, calibrated to the measured 45.08s Gopal
  read; enumeration may consume the first call.
  A new account starts only if its full budget fits. Completed outcomes are
  persisted atomically; an in-flight interruption is never automatically replayed.
  After inspection, `retry_current=true` checks the active account without
  selecting it again; `continue_after_blocker=true` skips the failed/uncertain
  account. The options are exclusive. Skipped accounts remain failures and the
  audit reports partial, never complete.
- Updated packaged skill, MCP prompt/schema, CLI flags, README, installed
  `~/.agents/skills/mobile-use` and `~/.commandcode/skills/mobile-use` copies.
- `scripts/benchmark_facebook_audit.py` and targeted regression tests.

Full Python suite passed: 505 passed, 2 skipped; JavaScript suite: 90 passed.
The unlocked live run enumerated all ten accounts in 37.99 seconds and
verified Heer HS (Manila, Metro Manila 1001), Gopal Gopal Katheriya (Lahore,
Punjab 54), and Afiza Parween (Muridke, Punjab 39). Afiza recovered after a
delayed login without replaying selection. Tahir's attempted login timed out
and later identity verification found Afiza still active, so no result was
attributed to Tahir. Shyam's login remained on its spinner after the initial
50-second deadline and one additional guarded 50-second wait. A subsequent
attempt to reach Purnank stopped without selection because Shyam was still
logging in; the remaining four accounts were not attempted.

Twelve captured audit calls consumed 336.80 seconds (336.88 seconds of captured
call wall time), including failed attempts and recoveries. This excludes
interactive debugging, screenshot inspections, edits/tests, and user pauses.
It is not a completed ten-account benchmark and does not establish a full-run
speedup. Captured results remain resumable; final device screenshot shows
`Logging in as Shyam Desai…`. Existing CommandCode MCP connections must reload
to load the new tool implementation.
In-flight hierarchy/ADB reads can exceed a very short remaining deadline;
new actions are bounded by deadline checks. Live Gopal and Afiza reads each
used the one-time blank-page recovery and returned identity-verified results.
Additional live regressions fixed hidden feed navigation and partially labeled
top six-tab bars. Facebook's prolonged login spinner remains an external
blocker; no account selection is repeated to force progress.

## Follow-up session and fixes (2026-10-04)

Session `2e7492fd-8d51-4e6d-b0ed-0d3442c6617a` lasted 25 minutes
20 seconds with 57 assistant turns. Facebook tool durations totaled about
341 seconds; the remaining time cannot be assigned precisely to screen reads,
model generation, or runtime overhead from the transcript alone. The updated
audit schema was loaded, but string arguments were rejected, CLI fallback
failed, and expired sessions and pending spinners triggered recovery overhead.

Only Shyam Desai (Lahore, Punjab 54), Afiza Parween (Muridke, Punjab 39),
and Gopal Gopal Katheriya (Lahore, Punjab 54) had verified location results.
Expired dialogs were observed for Heer HS, Tahir Shah, Purnank Pawshe, and
Aarti J Aarti J. The transcript did not establish a logged-out or expired
status for Saidu Kanu, Sada Sada, or Gurmukh Garima Singh Singh. Their absence
from a logged-out saved-card screen is insufficient evidence.

The follow-up fixes accept canonical numeric/boolean strings at the MCP
boundary while retaining strict validation in the audit module, restore
the Python CLI's `call` fallback, detect expired sessions promptly, and
distinguish saved sign-in cards from active sessions. Audit continuation
checks readiness without taps; unattempted accounts remain unchecked when
a session blocker occurs before selection. These changes need a new live
benchmark to establish their effect on total duration.

Validation: 527 Python tests passed, 2 skipped; all 90 JavaScript tests passed.
The Python console-script fallback's help was exercised without touching the
phone. Both installed mobile-use skills were synchronized. Restart the
CommandCode MCP connection before the next live audit so it loads these fixes.

The next CommandCode run exposed an overly strict startup rule: the workflow
treated every saved-account landing as requiring a manual sign-in, although
the user's audit request already authorizes trying an observed saved card.
The startup path now attempts a single uniquely recognized card, verifies its
active identity, then enumerates the full in-app picker. Location requests can
select the exact observed requested card. Read-only readiness and recovery
checks remain read-only; actual credentials, verification, expired sessions,
and unresolved transitions still stop the flow. Saved cards continue to be
reported as remembered accounts until sign-in and identity verification succeed.
The audit also records the already-active account correctly without selecting
it again. Validation after the startup change: 533 Python tests passed, 2 skipped;
all 90 JavaScript tests passed. Both installed mobile-use skills were updated.

## Original session results

| Account | Location shown | Verification in session |
| --- | --- | --- |
| Shyam Desai | Lahore, Punjab 54 | Tool returned identity_verified=true |
| Tahir Shah | Lahore, Punjab 54 | Manual switching/identity inspection |
| Afiza Parween | Muridke, Punjab 39 | Manual switching/identity inspection |
| Heer HS | Manila, Metro Manila 1001 | Manual switching/identity inspection |
| Gopal Gopal Katheriya | Lahore, Punjab 54 | Manual switching/identity inspection |

No location result recorded for Purnank Pawshe, Saidu Kanu, Aarti J Aarti J,
Sada Sada, or Gurmukh Garima Singh Singh.
