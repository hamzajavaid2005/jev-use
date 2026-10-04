---
name: facebook-create-pages
description: Create Facebook Pages across saved accounts in an existing GoLogin profile using a reusable runner, verified creation checkpoints, and logout between accounts.
---

Use this skill for the user's authorized Page-creation task. Keep GoLogin as the
browser owner. Use the jev-use MCP tools and their returned port; never start a
second browser, clear cookies, change proxy/fingerprint settings, or use a shell
CDP driver while MCP is attached.

The executable [scripts/account-runner.js](scripts/account-runner.js) is bundled
and injected into `browser_script` as `facebookPages`. Invoke it directly; do not
rewrite the login, category, creation, wizard, or logout logic for each account.
No live mutation is authorized merely by loading this skill.

## Setup once

1. Load `browser_profiles`, `browser_open`, `browser_script`, `browser_close`.
2. Find the exact requested GoLogin profile; open with `vendor="gologin"` and
   `headless=false` if needed. Reuse its port throughout the task.
3. Inspect the saved-account chooser once. Use the observed accounts and the
   requested count. Create one stable run ID and an account/Page-name mapping.
   If names are left to you, choose them once and keep them on retries.
4. Reuse the task-supplied password only for login; do not save credentials in
   the runner, run IDs, journals, screenshots, or files.

Use the complete profile list returned by `browser_profiles`; it handles API
pagination. Do not search local GoLogin metadata or guess IDs to replace that
list. When the user says "all GoLogin profiles", select every exact profile ID
returned by that call without asking a profile-scope question, sort by the
numeric Profile number, and explicitly open `Profile 1` first. Never start with
the API's newest/first returned item (for example `Profile 22`) or any other
profile merely because it appeared first in the response. When the user
supplies an ordered password list without an account mapping, assign password
index `n` to saved-account card index `n` within each profile. Do not cycle or
reuse a password after the list ends: mark extra accounts `missing_password`,
skip them, and continue. Keep credentials out of files and diagnostics.
For an `Invalid request` tool failure, identify the tool and check its published
schema, correct the arguments, and retry once. Preserve checkpoints and report
the failed tool and error if it persists rather than claiming a transient cause.

### Batch contract

The batch controller accepts an optional runtime-only mapping with this shape:

```js
{
  profiles: [{ id: "<exact GoLogin profile id>" }],
  accounts: [{
    profile_id: "<exact GoLogin profile id>",
    account: "<observed Facebook account name or id>",
    page_name: "<stable page name>",
    password: "<runtime value>"
  }]
}
```

If no mapping is supplied, it builds that mapping from the complete profile list,
the observed saved-account card order, and the ordered password list supplied in
the request. It enumerates all profiles, processes each exact profile once,
processes every account with a matching password, skips and records
`missing_password` or `mfa_required`, calls `browser_close`, and only then opens
the next profile.
Passwords are accepted only in the invocation memory; they must never be placed
in source files, environment files, checkpoints, journals, screenshots, logs, or
page names. Passwords are never guessed, rotated, or reused after the ordered
list is exhausted.

## Two calls per account

Use the same config and checkpoint scope in both calls. `account` is a stable
account identifier or the observed saved-account name; `account_name` is needed
only when those differ. The runner checks the active identity on resumption.

First call: `browser_script(port=N, timeout=240, code=...)`:

```js
return await facebookPages.prepare({
  run_id: RUN_ID,
  account: ACCOUNT,
  page_name: PAGE_NAME,
  bio: BIO,
  password: TASK_PASSWORD
});
```

Replace uppercase variables with task values in the invocation, without storing
the password in an external file. Keep default `dismiss_overlays=true`; the
Sign in as credential chooser is distinct from the Facebook password form.

Preparation verifies an already-signed-in account using its identity and matching
self-profile link, or selects the saved card once and waits up to 60 seconds for
password/remembered login to finish. It scrolls/plays the feed for 30 seconds
(tries an observed Reels/Videos link once if the home feed has no playback),
fills the Page form, waits through the configured post-fill settling delay,
verifies it is ready, and saves `submission_reserved` without creating the Page. The
bundled runner defaults to a 90-second browse, a 15-second post-fill delay, and
a 30-second home-feed scroll after creation confirmation;
override `browse_seconds` and `post_fill_delay_ms` deliberately when needed. If the user
changes the browsing requirement, adapt that step deliberately rather than
claiming it occurred. No observed playback means pause for one focused inspection.

If preparation returns `mfa_required`, record that account as skipped, leave its
checkpoint intact, and continue with the next saved account or GoLogin profile.

If `submission_reserved`, second call:

```text
browser_script:
  port: N
  timeout: 180
  submission: {run_id: RUN_ID, account: ACCOUNT}
  code: return await facebookPages.finish({run_id: RUN_ID, account: ACCOUNT, page_name: PAGE_NAME});
```

The submission argument checks actionability first, journals the attempt, and
clicks Create Page's button role once (works for `div[role="button"]`). The runner
then waits for the exact delayed success notice, accepts the optional wizard's
beforeunload, logs out through the account menu, and verifies the chooser returns.
Continue to the next account only after `logged_out`.

For a multi-profile batch, call `browser_profiles` once, iterate the returned
GoLogin profiles one at a time, and close each profile with `browser_close` after
its account list is exhausted. For each profile, continue through all saved
accounts; when `prepare` returns `mfa_required`, record the account as skipped
and move to the next account without retrying or clearing its checkpoint. Then
open the next GoLogin profile and reuse the same workflow.

## Resume and unexpected UI

- `logged_out`: account complete; do not create again.
- `mfa_required`: two-step verification is required; skip this account and continue the batch.
- `created`: run `facebookPages.finish` WITHOUT `submission` to finish logout.
- `submitting`: run `facebookPages.finish` WITHOUT `submission` to confirm and
  log out. If the notice is gone, inspect the actual Page identity once and use
  an observed confirmation selector/URL/ID. An unchanged URL, disabled button,
  search match, or "already manage a Page" error does not authorize another click.
- `submission_reserved`: inspection does not consume it. A missing selector
  fails before clicking, so correct it once and retry the submission argument.
- Other stages: correct the observed problem and retry preparation with the SAME
  run ID/account. Never erase checkpoints or generate new IDs to bypass a lock.

Never manually seed `accountId` to skip login. If the self-profile link cannot
verify the requested account, inspect once; an `account_id` supplied by the user
or verified in that inspection may be passed in config. The cookie must match it.
A login already selected/submitted resumes waiting without repeating the action.

Selector overrides in the runner are only for demonstrated UI changes:
`account_selector`, `name_selector`, `category_selector`,
`option_selector`, `bio_selector`, `create_selector`; finish accepts `confirmation`
and `logout` options from the workflow helpers. Pass `create_selector` in the
submission argument as `selector` too. Do not guess selectors or URLs.
For externally opened profiles, keep a stable non-secret `checkpoint_scope` on
all calls; profiles opened by this MCP server use the GoLogin ID automatically.
If an older checkpoint lacks `accountId`, inspect identity before migrating it;
never infer that it is the currently signed-in account.

Stop after one focused inspection/correction if the issue remains unresolved.
Hand off MFA/CAPTCHA/account restrictions or unsupported native dialogs. Never
bypass creation guards with a manual click, probe hidden APIs, clear cookies, or
perform a logout URL experiment. Batch known steps rather than narrate each click.

If login stalls, report the runner's `loginObservation` and any tool warnings.
Saved-account cards are not proof of live sessions; an absent identity cookie
and unchanged page do not prove expired sessions or restrictions. A native-dialog
connection warning means DOM inspection cannot rule out a browser-owned popup.
Inspect the visible browser once (including browser chrome), or hand off that
inspection when unavailable. Do not click behind a suspected native popup, test
other accounts to generalize the failure, or describe untested accounts as blocked
by a proven account-specific cause. Preserve the same run and checkpoints.

## Finish

Call `browser_close` to save the GoLogin profile. Report each account's Page name,
confirmed stage, captured Page URL/ID when available, and unfinished work. Include
elapsed time and the returned `timings` (login, browsing, navigation, fill,
confirmation, logout); do not call pending creation or pending logout complete.
