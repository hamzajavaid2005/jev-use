# jev-use

Global browser and Android tools for agent harnesses. Chrome runs in the
background; Jev chooses controls for ambiguous tasks. Exact actions and page
reads run directly without a decision-model request.

## Install once per device

Requires Node 18+. The installer provisions Python, the CDP client, optional
Jev SDK, and the driver fallback. It registers detected harnesses in their
user configuration and installs `/browser-use` and `/mobile-use` globally.
No repository folder needs to be open when using the tools.

Linux/macOS:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.sh)"
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.ps1 | iex
```

Repair/register Command Code:

```text
jev-use install --harness=commandcode
jev-use doctor
```

`doctor` performs an MCP handshake and lists tools, then tests Command Code's
registered launcher (including its Windows shell behavior). It reports startup
errors instead of treating a config file as proof of a connection. A successful
check verifies the launcher, not the desktop app's live connection; use `/mcp`
or **Test connections** in the app after restarting it. MCP startup never installs
or repairs Python; if the runtime is missing, run `jev-use install` first.

Set keys once:

```text
jev-use install --key=<typesafe-key>
jev-use install --gologin-token=<gologin-token>
```

Restart the harness after installation. Supported registration adapters:
Command Code, Claude Code, Codex, Cursor, OpenCode, Windsurf, Gemini CLI, VS Code.
Slash-command discovery depends on the harness's skill support. Shared skills
are installed under `~/.agents/skills` (Windows: `%USERPROFILE%\.agents\skills`).
Harnesses installed later need another `jev-use install` to register MCP.

```text
jev-use harnesses
jev-use harnesses --print
```

The second command prints configuration for other MCP-capable harnesses.
For a private repository, installers need GitHub access. To install a specific
revision, download `install.ps1` and run it with `-Ref <commit>`; on POSIX use
the bootstrap's `--ref=<commit>` option. Do not install the unrelated public
registry package named `jev-use`. Install this repository's archive or a packed
tarball. Avoid direct npm git installs that can register temporary cache paths.

## Choose the shortest tool path

Call `browser_profiles` once. Reuse the matching port, or call `browser_open`
with the requested profile. Use its returned port in later calls.

| Need | Tool | Uses Jev? |
| --- | --- | --- |
| Read a known URL | `browser_read(port=N, url="https://...")` | No |
| Read several URLs in parallel | `browser_read_many(port=N, urls=[...])` | No |
| Click an exact label | `browser_action(port=N, action="click", label="Save")` | No |
| Fill an exact field | `browser_action(port=N, action="fill", label="Email", text="...")` | No |
| Scroll/back | `browser_action(port=N, action="scroll_down")` or `action="back"` | No |
| Prepared multi-step workflow | `browser_script(port=N, code="...")` | No |
| Ambiguous or multi-step goal | `browser_use(port=N, goal="...", act=true)` | Yes, when needed |
| Extract typed answers | `browser_extract(port=N, questions={...})` | Yes |

`browser_action` acts once and returns page text. Click/fill require exactly one
matching visible control; ambiguous labels are refused. `browser_use` validates
Jev's choice against real controls; exact deterministic decisions and cached
plans bypass Jev. Use `decompose=false` for one-step goals. Compound planning
and generated text require a configured `JEV_USE_TEXT_MODEL`; an unavailable
planner is skipped. Literal text supplied to `browser_action` needs no writer.

For repeated Facebook Page creation, use `/facebook-create-pages` (or
`$facebook-create-pages` in Codex). Installation copies its instructions, runner,
and metadata into the shared and supported harness skill directories. The runner
is also injected into `browser_script` as `facebookPages`, so the agent calls
`facebookPages.prepare` and `facebookPages.finish` instead of rewriting the flow.
It uses the existing GoLogin browser, the requested 30-second feed browsing step,
and the submission checkpoint protocol. It never stores the task password.

For a known workflow, `browser_script` runs prepared Playwright through the
BetterWright SDK, avoiding a model request per click. It uses Node.js 22+ and the
installed BetterWright package; Bun is no longer required at execution time.
GoLogin still launches and saves the original profile. Call `browser_close`
afterward to save its state.

The SDK connection, selected tab and `state` persist between script calls. If
several tabs exist at first attachment, supply `page_url` with the exact existing
workflow URL; omit it afterward to follow the same tab through navigation.
Switching to interactive tools detaches the script worker, so keep a prepared
workflow on the script path. A script failure is an MCP error, not a success.
Do not replay a timed-out submission without checking whether it committed.

Scripts dismiss recognized page overlays and the scoped Facebook `Sign in as`
dialog. The selected tab also has native FedCM account chooser monitoring via
CDP. Set `dismiss_overlays=false` to preserve these choosers. Password forms,
other tabs and unrelated native dialogs are untouched. Unsupported native dialog
monitoring is reported as a warning; other browser-owned UI may require manual
dismissal. Prepare `dialogs.dismissNext()` or `dialogs.acceptNext()` before
an action that opens a JavaScript dialog.

Inspect selectors once, batch deterministic stretches, and observe when the form
structure changes. A quick connection check requires no page mutation:

```js
return { url: page.url(), title: await page.title(), stateAvailable: !!state };
```

Script deadlines are in seconds (1–900, default 120). `browser_use.settle` is
also in seconds, limited to 0–10; `5000` is rejected immediately. The MCP server
answers heartbeat and schema requests while a tool runs and refuses overlapping
actions instead of silently queueing duplicate submissions. Attachment errors must be diagnosed from the returned error; they do not prove CDP supports only one websocket.

Prepared scripts expose a checkpointed `workflow` helper for repeated account
jobs. It supports `workflow.begin`, condition-based login detection,
`workflow.browseFeed({seconds:90})`, `workflow.beforeCreate`, `workflow.submitCreation`, exact creation
confirmation, and `workflow.loggedOut`. Checkpoints are written atomically so a
timeout or logout failure stops a duplicate submission. The 30-second browsing
period is used for repeated Facebook Page-creation jobs, and is never added to
ordinary browser tasks. The helper reports whether video playback was observed;
scrolling alone is not reported as successful playback.

Use `workflow.loginSavedAccount` for the saved-account chooser (password or
remembered session) and `workflow.fillPage` for the observed form. Call
`workflow.beforeCreate(pageName)` after the form is actionable, then submit via
`browser_script`'s `submission:{run_id,account}` argument. Reads preserve the
reservation. An invalid selector fails before any click or consumption of the
reservation. The tool checks actionability using a trial click, saves the attempt
on disk, then clicks the button role once. The snippet runs after the click:

```js
await workflow.confirmCreated();
return await workflow.logout();
```

Confirmation waits for a delayed creation notice containing the exact Page name.
Logout accepts the wizard's beforeunload and uses the account menu, never clears
cookies or invents logout tokens. After a submission, only re-check confirmation
or logout; a disabled button or unchanged URL does not authorize another click.
Use stable run/account IDs on retries and checkpoint_scope for externally opened
profiles. The helper selectors can be overridden using observed DOM evidence.

On Windows, a browser-owned `Sign in as` chooser may be outside the page DOM.
The script worker starts a scoped PowerShell UI Automation helper that invokes
`Close` only when it finds that heading inside the same browser process. It does
not send global keystrokes or click unrelated windows; warnings are returned if
the helper cannot access the dialog.

Reads default to 6,000 characters per page; `max_chars` can raise this to 20,000.
The harness receives 15 tool schemas, loaded skill instructions, and tool results;
it does not receive the project source. Actual context usage depends on the
harness's tool discovery and tokenizer.

## Background work

`browser_open` launches a visible browser by default. Only explicit `headless=true`
opts into headless mode, for Chrome or GoLogin. Legacy `background=true` no longer
hides the browser. The shell fallback accepts `--no-headless` for visible mode.
An existing headless session must be closed and reopened to show a window.
Opening a visible window may briefly take focus; ongoing
CDP actions do not bring it to the foreground. Known URL tasks use a dedicated background tab; parallel reads pin
each tab by ID. CDP actions do not move the system mouse or send OS keystrokes,
so the user can work in other windows. Do not automate the same page the user
is editing. Sites that open native dialogs may still need user interaction.

Chrome profiles are copies in a private data directory because Chrome blocks
CDP on its default directory. Copies are snapshots; `refresh=true` updates them.
Copying a large profile is a one-time cost, not a model delay. On Windows,
installed Chrome takes precedence over PATH/registry entries that may point to
Orbita. `JEV_USE_BROWSER_BINARY` selects an explicit browser executable.

GoLogin uses its own SDK and Orbita with the profile's fingerprint, proxy and
cookies. Use `browser_open(profile="name", vendor="gologin")`, then
`browser_close()` to stop and save the profile. Its native launch is preserved;
`headless=true` requests no visible window. Cloud debugger URLs are unsupported;
this transport expects a local CDP port. Get a token from
<https://app.gologin.com/#/personalArea/TokenApi>.

## Android

Install Android platform-tools (`adb`) and accept USB debugging on an unlocked
phone. `scrcpy` is optional. Start with `android_devices`, then `android_read` or
`android_use(goal="...", act=true)`. Read again to verify actions.

Pass `serial` when multiple devices are attached. `android_location` reads
Facebook's account location; quote it only when `state=location`. Typing supports
ASCII and generated text requires a text model. Phone automation can run while
the user works on the PC, but shares the phone's visible screen with manual use.

For Facebook account batches, use `android_facebook(action="audit")`. It
enumerates saved accounts and processes them sequentially in bounded chunks.
For an authorized login → primary-location read → Public location check-in →
logout job, use `android_facebook_flow` with the exact account/place,
`publish=true`, and a stable `run_id`. It follows observed controls without a
decision model per tap and journals submission before posting. Reuse its
`resume_token` to continue bounded calls; `complete=true` confirms both the post
and logout. The check-in is separate from Facebook's inferred primary location.
Continue with the returned resume token while `complete=false`; keep each call
sequential. If a blocker stops the audit, inspect the phone and follow its
detail. For an observed delayed login, pass the same token with
`retry_current=true` (CLI: `--retry-current`). Recovery verifies the currently
active account and reads its location without selecting the saved account again.
Use `continue_after_blocker=true` (CLI: `--continue-after-blocker`) instead if
the inspected account must be skipped. The two flags are mutually exclusive.
The skipped account remains unverified.
Each result includes the account, location, identity verification, state, and
elapsed time. Quote a location only when `state=location` and
`identity_verified=true`. The workflow returns explicit blocked states for
locks, connection failures, sign-in, and unsupported layouts. It does not edit
location or publish posts. For a single account, `action="location"` remains
available.

To summarize captured audit calls, save one JSON object per line in a JSONL
file. Each line can be the tool response directly or a JSON object with the
response under `response` and optional `wall_seconds`, `tool_calls`, and
`model_calls` numbers from the harness. Then run:

```text
python scripts/benchmark_facebook_audit.py audit-responses.jsonl
```

The report counts unique verified accounts, reported location retries, per-account
elapsed time, audit call time, and known wall time. Audit responses include
cumulative results, so the script counts each account once. Enumeration time is
shown separately because it is already included in the first audit call. Record
wall time around each tool call to
make it useful. It leaves model/provider latency unknown when the harness does
not report it; the wall-minus-call figure is unattributed time, not an inference
measurement. Compare a live complete run with the 43m20s historical session
only after reporting verified completion count. The 5x improvement in the plan
is an engineering target, not a measured result.

`android_read(include_screenshot=true)` returns both tappable descriptions and
the phone image for unfamiliar UI. Exact unique descriptions can be selected
with `android_use(goal="tap EXACT_DESCRIPTION", act=true, max_steps=1,
decompose=false, use_cache=false)`. Reload the MCP connection after updating so
the new tool schemas and handlers are active.

## Missing MCP tools

Use the built-in fallback instead of asking the model to write clients:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default
jev-use call browser_read --port 9222 --url https://example.com
jev-use call browser_action --port 9222 --action click --label Save
jev-use call android_devices
jev-use call android_use --goal "open Settings" --act
```

Use the actual returned port. `--json-file <path>` supplies advanced arguments
without shell quoting problems. The fallback starts a process per command;
native MCP reuses a session and is faster for repeated calls. Search for missing
tools once, use the fallback, and report persistent failures instead of repair
loops. `doctor` checks configuration and installed paths; it does not prove the
app connected its MCP session.

If `jev-use` is not on PATH but this repository checkout is available, run the
same fallback from the repository root with `.venv/bin/python -m jev_use call <tool> ...`
using the checkout's environment, or the interpreter where jev-use is installed. The Python
console script also accepts the `call` subcommand directly.

Facebook audits can start from the saved-account sign-in screen: the workflow
tries one uniquely observed saved card, verifies the resulting active identity,
then enumerates the full in-app picker. A saved card alone never establishes a
valid session. Password, verification, expired-session, and pending-login
blockers remain explicit; an uncertain selection is never automatically replayed.

## Development

```text
pip install -e '.[dev,harness]'
python -m pytest -q
npm test
```

Source and historical design notes: [docs/architecture.md](docs/architecture.md).
The npm install excludes retired desktop modules and development tests.
