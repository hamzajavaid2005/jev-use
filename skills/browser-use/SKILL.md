---
name: browser-use
description: Use Chrome or GoLogin to read sites and perform browser actions.
---

Use jev-use tools. Keep calls and output short.
Chrome and GoLogin open visibly by default. CDP actions do not move the user's
system mouse or keyboard; keep using the returned port while the user works in
other windows. Never bring the browser to the foreground or use OS input.
Known URLs use dedicated background tabs without activating them; the user can
select those tabs to watch. Launching a visible browser may briefly take focus.
Always pass `headless=false` when opening Chrome or GoLogin unless the user
explicitly requests headless. `background` is a legacy option, not headless mode.
An already-running headless browser cannot become visible through an action:
explain that it needs closing and reopening; do not silently reuse it if the
user wants a visible browser. Never kill an unrelated browser to do this.
Do not operate the same page the user is actively editing.

For repeated Facebook Page creation, load `facebook-create-pages` and use its
bundled `facebookPages` runner. That skill contains the focused account workflow
and recovery rules; do not recreate the script from scratch.

1. Call `browser_profiles` once. Reuse the matching live CDP port.
2. If closed, call `browser_open(profile="name", headless=false)` once and use its returned port.
   Use `vendor="gologin"` for GoLogin; never copy it into Chrome.
   For a public task with no requested account, use Chrome's Default profile.
   For an account task with several possible profiles, ask which account.
3. Reading a known URL: `browser_read(port=N, url="https://...")`.
   This navigates and reads in one call, with no decision-model request.
   Several URLs: `browser_read_many(port=N, urls=[...])`.
4. Exact action: `browser_action(port=N, action="click", label="Save")` or
   `action="fill", label="Email", text="user@example.com"`. Scroll uses
   `action="scroll_down"`. This acts once and returns text without a Jev call.
   For any repeated login, account switch, form, wizard, or checkout workflow,
   **MUST use `browser_script`** with one prepared BetterWright Playwright
   snippet per account. It attaches to the returned port, does not launch
   another browser, and avoids a model round-trip per click. Inspect once if
   selectors are unknown, then write the script and run it. Do not switch to
   agent-browser for the same page. Install it once with
   `bun install -g betterwright` and verify the script result.
   `browser_script` dismisses recognized cookie/promotional overlays and the
   Facebook `Sign in as` chooser shown with a `Close` button by default. It
   verifies that the chooser disappears before running the script. Set
   `dismiss_overlays=false` to keep it open. Password forms and unrelated dialogs
   stay open. If this popup appears midway through a script, repeat the same
   scoped dialog close and wait for it to disappear before continuing.
   Browser-owned FedCM account chooser alerts are monitored over CDP on the
   selected tab and dismissed when `dismiss_overlays=true`. Other browser UI is
   not guaranteed to be accessible; report warnings and request manual dismissal
   if it still blocks the task. Never repeatedly click behind a popup.
   For native JavaScript dialogs, prepare `dialogs.dismissNext()` or
   `dialogs.acceptNext()` before the action that opens it.
   Scripts run through Node.js 22+ (no Bun PATH dependency), retain `state` and the
   selected tab between calls, and use a hard timeout in seconds. If multiple tabs
   exist on first attachment, pass `page_url` with the exact observed workflow URL.
   Omit it afterward to retain the same tab through navigation.
   Do not mix script and interactive tools during a prepared workflow: interactive
   tools detach the script worker. Switching tools can also select another tab.
   On a script attachment error, stop and report the error. Never infer that CDP
   allows only one websocket, never launch a second shell driver as a workaround,
   and never fall back to dozens of goal-based clicks. After a timeout, inspect
   state before retrying: a submission may already have committed.
   `settle` is SECONDS, limited to 0–10: use `5`, never `5000`. Stop after two
   failed actions on the same control and inspect the real target.
   Repeated Facebook Page jobs MUST reuse the supplied workflow helpers, not
   re-discover the same flow or invent manual fallback clicks:
   - `workflow.begin(runId, account)` uses stable IDs across retries.
   - `workflow.loginSavedAccount({accountName,password})` selects the saved card,
     waits for either password OR signed-in state, and submits the password with
     Enter. Caller may override observed account/signed-in selectors if needed.
   - `await workflow.browseFeed({seconds:120})` before opening the Create Page form; the bundled runner waits through the pre-creation browse and scrolls the Facebook home feed for 30 seconds after creation confirmation.
     If no video plays, inspect Videos/Reels once; never invent playback.
   - `workflow.fillPage({pageName,bio})` fills name/category/bio, waits for the
     Reel creator suggestion, selects it, and verifies the Create Page control
     is actionable WITHOUT clicking. Do not use a keyboard category fallback.
   - `workflow.beforeCreate(pageName)` saves a reservation; end this call.
   - Submit through the tool's `submission:{run_id,account}` argument. Default
     control is the button ROLE named Create Page (including div[role=button]).
     The tool checks actionability BEFORE reserving the attempt on disk. Reads
     and selector-validation failures no longer consume submission permission.
     Code runs AFTER that one click: `await workflow.confirmCreated(); return
     await workflow.logout();` batches delayed confirmation and logout.
   - Confirmation waits up to 30 seconds for the exact creation notice. Never
     treat a disabled button or unchanged URL as proof of failure. Once clicked,
     retry only confirmation or logout. NEVER manually click Create Page again,
     clear the journal, change run IDs, or infer completion solely from an
     "already manage a Page" error. Observe the actual notice or Page ID/URL.
   - `workflow.logout()` accepts the wizard's beforeunload, navigates to the
     observed main Facebook URL, opens the Your profile button by role (no
     aria-haspopup assumptions), clicks Log Out, and waits for the chooser.
     Do not invent logout.php tokens, clear cookies, probe hidden APIs, or search
     for the created Page by name. Caller may override observed menu selectors.
   Typically use two calls/account: login+browse+fill+reserve, then tool submission
   with code confirm+logout. A confirmed selector mismatch permits one inspection
   and correction; do not inspect between every successful step. Start each
   phase with the same workflow.begin IDs after reconnecting. Use a stable
   checkpoint_scope when attaching to a profile opened elsewhere; this server
   uses the GoLogin profile ID automatically when it opened the profile.
   Keep default overlay dismissal enabled: the screenshot's Sign in as chooser
   is a blocking credential selector, separate from Facebook's password form.
   On Windows its scoped Close button is also handled through UI Automation.
   Do not disable dismissal just because the task includes login.
   If the target is ambiguous or needs several steps without a prepared script, use
   `browser_use(port=N, goal="...", act=true)`: Jev chooses from real controls.
   Keep goals short; use `decompose=false` for a single step. Verify the result.
5. After a GoLogin task call `browser_close()` to save its state.

If MCP tools are missing, search once, then use the built-in shell fallback:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default --no-headless
jev-use call browser_read --port 9222 --url https://example.com
jev-use call browser_action --port 9222 --action click --label Save
jev-use call browser_script --port 9222 --json-file workflow.json
jev-use call browser_use --port 9222 --goal "open billing" --act
```

Use the actual returned port. Ordinary shell arguments work on Windows too.
For advanced arguments use `--json-file <file>`.
Do not create MCP clients, CDP scripts, install packages, edit registry entries,
kill browsers, or repeatedly search for missing tools. If a call fails, retry
once only when the error is transient. Otherwise return the error and stop.
If MCP registration is missing, recommend
`jev-use install --harness=commandcode` and restart the app; use the fallback
for this task. Never spend the user's task debugging the installation.
