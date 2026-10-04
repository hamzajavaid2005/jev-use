---
name: mobile-use
description: Read and operate the user's Android phone through adb.
---

Use jev-use tools. Keep goals, calls and output short. Run device listing,
reading, location inspection, and actions sequentially; the MCP server runs one
tool at a time. Await each result before starting the next call.

## Facebook login, location, Public check-in, logout

Use `android_facebook_flow` for an explicitly requested full check-in job.
The tool follows the observed app controls deterministically; do not plan each
tap, invoke a decision model per step, or rewrite the flow. Read device listing
once and reuse its serial. Supply the exact saved account, requested place,
`audience="Public"`, `publish=true`, and one stable non-secret `run_id` for that
job. Reuse the same run ID on retries; each account uses its own checkpoint.

```text
android_facebook_flow(serial="SERIAL", account="EXACT_ACCOUNT",
  place="Manila, Philippines", audience="Public", publish=true,
  run_id="JOB_ID", timeout=55)
```

The flow signs in or verifies the active account, checks its primary location,
prepares the exact place tag, verifies Public visibility, submits once, confirms
the newly published account/place/Public feed card, then logs out and confirms
the saved-account screen. It returns one small result with timings and a
`resume_token`. For normal `timeout` in a reversible stage, pass that token and
the same arguments to continue without replaying login or rebuilding a draft.
`complete=true` means both publication and logout were verified. `publish=false`
checks location without publishing. Only the observed Public audience is
supported by this flow.

If the result is `requires_inspection` at `submitting`, inspect once and do not
tap Post again: it may already have committed. `logging_out` resumes by checking
the landing or an observed incomplete confirmation, without repeating the Menu
logout action. Preserve the checkpoint for actual password/MFA/lock/network or
unsupported UI blockers. A stalled login or blank page can use the single
restart recovery below, then resume the same flow token; never clear app data.
An unavailable primary location is reported as unverified, even when a separate
authorized check-in succeeds. A check-in does not set inferred primary location.

If this new tool is absent from the live catalog, reload the jev-use MCP process
or use the repository/installed built-in fallback; do not substitute repeated
model-guided taps:

```text
.venv/bin/python -m jev_use call android_facebook_flow --serial SERIAL --account "EXACT_ACCOUNT" --place "Manila, Philippines" --audience Public --publish --run-id JOB_ID --timeout 55
```

Add `--resume-token TOKEN` on continuation. The user’s instruction to publish
authorizes submission; do not add a confirmation step for an already specified
account, place, and audience. Clarify ambiguous posting requests before publishing.

## Facebook account location jobs

For checking every saved Facebook account, use `android_facebook(action="audit")`
when the live tool schema exposes it. Set `chunk_size=2` unless a smaller chunk
is needed to fit the harness timeout. Keep calls sequential; when `complete=false`,
pass the returned `resume_token` unchanged on the next audit call. Results are
cumulative, so preserve prior verified entries. If an audit response contains
a blocker, inspect the phone once and follow the returned detail. If the tool
attempted that account's switch and it finished late or is still logging in, resume
with the same token and `retry_current=true` (CLI: `--retry-current`). This waits
for that login, verifies the active identity, and reads without selecting again;
an identity mismatch stays blocked. If Facebook remains stuck on the login
spinner or a blank location page, close and reopen it once using
`android_facebook(serial="...", action="restart", account="EXACT_REQUESTED_NAME", timeout=50)`.
This force-stops and relaunches Facebook without clearing app data, verifies the
requested active identity, and reads its location without selecting a saved card
again. Keep the same audit resume token; after successful recovery, use
`retry_current=true` to record the verified result in that audit. Never restart
in a loop or replay an uncertain account selection. If restart is missing from
the live schema, reload MCP or use the installed fallback
`jev-use call android_facebook --serial SERIAL --action restart --account "EXACT_REQUESTED_NAME" --timeout 50`.
After inspection, if that account must be
skipped, pass the same token with
`continue_after_blocker=true` (CLI: `--continue-after-blocker`) to skip that
uncertain account and proceed with later accounts. The skipped account remains
unverified; never replay its selection automatically. For one account, or while
`audit` is unavailable, use the deterministic workflow below:

Starting on Facebook's saved-account landing screen does not require the user
to tap a card manually. `audit`/`accounts` may select one uniquely observed saved
card once to establish a session, verify its active identity, then enumerate the
full in-app account picker. `location` may select the exact requested observed
card. The user's account-check request authorizes these taps. Ask for help only
when the attempted sign-in actually needs a password, verification, unlocking,
or another unresolved blocker. Do not ask for approval just to select a card.

1. `android_devices()` once, then keep its serial.
2. `android_facebook(serial="...", action="accounts")` once. Use only returned
   account names. `state=accounts` means the list was observed and verified.
   Remembered cards prove only that accounts are saved, not that their sessions
   are valid. Missing cards on a logged-out landing screen do not prove that
   those accounts are logged out.
3. For each requested account, sequentially call
   `android_facebook(serial="...", action="location", account="EXACT_NAME")`.
   This handles menu navigation, selection, waiting for login, opening a fresh
   location page, and verification of the active identity before and after the
   read. It requires no decision model and leaves Facebook on the verified Menu.
4. Quote `location` only when `state=location` and `identity_verified=true`.
   Do not copy a result to another account or treat notifications/VPN location as
   primary-location evidence. Keep results keyed by returned `account`.

`locked`, `network_error`, `login`, and `authentication_required` require the stated user action. For
`identity_mismatch`, `timeout`, `unsupported_ui`, or `account_ambiguous`, inspect
once with `android_read(include_screenshot=true)`. Use the guarded audit recovery
above for an observed delayed login; otherwise stop. Do not try a manual
guessed-tap fallback, repeat a timed-out account selection, or lower model
confidence. The operation may have taken effect. Unknown layouts fail without
guessing. A partial list is never reported as complete. Preserve already confirmed
results on interruptions and resume when a token is available.

`session_expired` and `pending_login` are session blockers.
Stop on an expired dialog. For a pending spinner that remains stuck after the
bounded login wait, use the single close-and-reopen recovery above; if it stays
blocked afterward, stop and report the observed state. Do not attribute it to later
accounts. Report `unattempted_accounts` as unchecked. Resolve the observed
session blocker before continuation; the tool checks readiness without taps
and preserves the checkpoint if Facebook is still blocked. Use `retry_current`
only for an attempted account, never to bypass a global session blocker.
The read-only continuation readiness check can still return `saved_accounts`;
that reports no verified active session. The startup workflow above can attempt
saved-card sign-in. Never replay an uncertain selection or call remembered
cards verified sessions before identity verification succeeds.

Primary location is Facebook's inferred value; these read tools do not set it.
A request to update a profile city or make a location-tagged post is a separate
UI action. Do not invent a primary-location write operation, assume fraudulent
intent, or decline solely because accounts share a requested city. If “post
location” is ambiguous, clarify which UI action the user wants.

The running MCP process must be reloaded after installing updated tools. If
`android_facebook` is absent from the live tool catalog, use the built-in
`jev-use call android_facebook --serial SERIAL --action audit --chunk-size 2`
fallback. If `jev-use` is not on PATH, run the equivalent from the repository
root as `.venv/bin/python -m jev_use call android_facebook --serial SERIAL --action audit --chunk-size 2`
when the checkout has its virtual environment; otherwise use the Python
interpreter where jev-use is installed. Continue with `--resume-token TOKEN`
from the returned response. Use `--continue-after-blocker` only after
inspecting/resolving the blocker. The
tool-call fields are `chunk_size`, `resume_token`, `retry_current`, and
`continue_after_blocker`. The recovery and skip flags are mutually exclusive.
Do not revert to model-guessed taps after an unsupported layout.

## Other phone tasks

1. `android_devices()` once. No device: ask to connect it. Unauthorized: ask to
   unlock and accept USB debugging. Several devices: pass `serial` explicitly.
2. Read the current screen with `android_read(serial="...")`. It includes
   tappable control descriptions, even when an icon has no accessibility label.
3. Perform the requested action with
   `android_use(serial="...", goal="...", act=true)`.
   Use `decompose=false` for a single step. Then `android_read` to verify.
   For a known control, use `goal="tap EXACT_DESCRIPTION"`, `max_steps=1`,
   `decompose=false`, and `use_cache=false`. Exact unique descriptions are selected
   without a model guess. Re-read after navigation; descriptions are not durable
   IDs. For unlabelled icons, inspect the actual screen to establish which control
   serves the goal; do not infer an icon's meaning from its position alone or
   repeatedly rephrase a goal after low confidence. Do not lower confidence to
   force a guess.
4. For Facebook's account location use `android_location`. Report `location`
   only when `state=location`; `state=login` means sign-in is required.

If MCP tools are missing, search once, then use the built-in shell fallback. If
`jev-use` is not on PATH, run the command from the repository root as
`.venv/bin/python -m jev_use call <tool> ...` using the checkout's environment:

```text
jev-use call android_devices
jev-use call android_read
jev-use call android_use --goal "open Settings" --act
```

Use `--serial <id>` when needed and `--json-file <file>` for advanced arguments.
Do not write helper clients, install packages, or loop over failed calls.
A "Request not started" busy response means this call was rejected, not that
its requested action is running. Wait for the named original call to finish.
Do not poll by starting another action/read or assume an unchanged screen proves
a hang. If the original call times out, report its error and inspect once after
the server is available; never repeat an uncertain state-changing action.
Retry a transient failure once; otherwise report it and stop.
The phone must be unlocked. Typing requires a configured text model and supports
ASCII only. scrcpy is optional. Quote what the screen shows; do not invent values.
