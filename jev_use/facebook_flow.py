"""Checkpointed deterministic Facebook check-in orchestration.

Account selection and location reads reuse the Facebook adapter. The check-in
and logout use controls observed in the live Android app, with durable journals
before submission. No model chooser or generated text is needed.
"""
from __future__ import annotations

import time
import re
from typing import Any

from . import android, facebook_android, facebook_flow_state
from .facebook_android import Blocked


def prepare_checkin(workflow: facebook_android.Workflow, *, token: str, place: str,
                    audience: str) -> dict[str, Any]:
    """Open and fill a check-in using observed controls.

    Every tap is resolved against the current accessibility hierarchy. The
    function stops at a verified preview with an enabled Post button; it never
    submits the post.
    """
    if facebook_android.normalize(audience) != "public":
        raise Blocked("unsupported_ui", "Only the observed Public audience flow is currently supported.")
    observation = workflow.observe()
    account = facebook_flow_state.load(token)["account"]
    if any(_norm(line) == "who can see your post?" for line in observation.read().splitlines()):
        observation = _finish_audience_picker(workflow, observation, account=account, place=place)
    if _is_preview(observation, workflow, account=account, place=place):
        observation = _require_public_audience(
            workflow, observation, account=account, place=place)
        return _save_ready_draft(token, workflow, observation,
                                 account=account, place=place, audience=audience)

    picker_open = _picker_open(observation)
    composer_open = _composer_open(observation)
    if not picker_open and not composer_open:
        # Verify identity from Menu before navigating to the feed. A resumed
        # composer/picker is handled in place so its draft is preserved.
        active = facebook_android.identity(observation)
        if not active:
            observation = workflow.menu(observation)
            active = facebook_android.identity(observation)
        _require_account(active, account)
        home = _home_tab(observation)
        if home is None:
            raise Blocked("unsupported_ui", "No unique observed Home tab or complete six-tab row was found.")
        _tap(workflow, home, "Facebook Home tab")
        observation = workflow.wait(
            lambda obs: bool(_exact_targets(obs, "What's on your mind?", role="button"))
            or bool(_exact_targets(obs, "Go to profile")),
            "Facebook Home did not expose a recognizable feed.")
        prompt = _exact_targets(observation, "What's on your mind?", role="button")
        if not prompt:
            # Facebook can hide the feed controls below the fold. This single
            # bounded scroll is allowed only with the observed profile marker.
            profile = _exact_targets(observation, "Go to profile")
            if len(profile) != 1:
                raise Blocked("unsupported_ui", "The feed marker is ambiguous; the composer was not opened.")
            workflow.ensure_time()
            android.scroll(workflow.serial, observation.screen, "scroll_up")
            observation = workflow.observe()
            prompt = _exact_targets(observation, "What's on your mind?", role="button")
        if len(prompt) != 1:
            raise Blocked("unsupported_ui", f"Expected one observed feed composer button; found {len(prompt)}.")
        _tap(workflow, prompt[0], "feed composer")
        observation = workflow.wait(_composer_open,
                                    "Facebook's New post composer did not open.")
        composer_open = True

    if composer_open:
        _require_composer_account(observation, account)
        if _has_current_location_tag(observation, place):
            pass
        else:
            location = _location_button(observation)
            if location is None:
                raise Blocked("unsupported_ui", "The composer has no unique observed location-tag control.")
            _tap(workflow, location, "composer location tag")
            observation = workflow.wait(_picker_open,
                                        "The Add location picker did not open.")
            picker_open = True

    if picker_open:
        card = _matching_place_card(observation, place)
        if len(card) != 1:
            search = _search_field(observation, place)
            if search is None:
                raise Blocked("location_not_found", "The exact requested place is not listed and no unique Search field is available.")
            node = _node_for_target(observation, search)
            current_value = (node.text or "").strip() if node else ""
            if (current_value and _norm(current_value) not in
                    (_norm(place), "search")):
                raise Blocked("unsupported_ui", "The Search field contains a different query; it was not overwritten.")
            if _norm(current_value) != _norm(place):
                if not android.is_ascii(place):
                    raise Blocked("unsupported_ui", "The place name contains non-ASCII text that Android input text cannot enter safely.")
                _tap(workflow, search, "place search field")
                workflow.ensure_time()
                android.type_into(workflow.serial, place)
                observation = workflow.wait(
                    lambda obs: bool(_matching_place_card(obs, place)),
                    "Facebook did not show one exact place result after searching.")
            card = _matching_place_card(observation, place)
        if len(card) != 1:
            raise Blocked("location_ambiguous", f"Expected one exact {place!r} location card; found {len(card)}.")
        _tap(workflow, card[0], "exact location result")
        observation = workflow.wait(
            lambda obs: _composer_open(obs) and _has_current_location_tag(obs, place),
            "The composer did not confirm the requested location tag.")
        _require_composer_account(observation, account)

    if _composer_open(observation):
        _require_composer_account(observation, account)
        if not _has_current_location_tag(observation, place):
            raise Blocked("location_unverified", "The composer did not show the exact requested location tag.")
        next_button = _exact_targets(observation, "Next", role="button")
        if len(next_button) != 1:
            raise Blocked("unsupported_ui", f"Expected one enabled Next button; found {len(next_button)}.")
        _tap(workflow, next_button[0], "composer Next")
        observation = workflow.wait(
            lambda obs: _is_preview(obs, workflow, account=account, place=place),
            "The post preview did not confirm the requested account and place.")

    if not _is_preview(observation, workflow, account=account, place=place):
        raise Blocked("unsupported_ui", "The current screen is not a verified post preview.")
    observation = _require_public_audience(workflow, observation,
                                           account=account, place=place)
    _verify_post_button(observation)
    return _save_ready_draft(token, workflow, observation,
                             account=account, place=place, audience=audience)


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _exact_targets(observation: android.Observation, label: str,
                   *, role: str | None = None) -> list[dict[str, Any]]:
    return [target for target in observation.targets
            if _norm(target.get("label", "")) == _norm(label)
            and (role is None or target.get("role") == role)]


def _tap(workflow: facebook_android.Workflow, target: dict[str, Any], action: str) -> None:
    workflow.ensure_time()
    centre = target.get("centred")
    if not (isinstance(centre, (tuple, list)) and len(centre) == 2):
        raise Blocked("unsupported_ui", f"The observed {action} control has no usable tap bounds.")
    android.tap(workflow.serial, int(centre[0]), int(centre[1]))


def _home_tab(observation: android.Observation) -> dict[str, Any] | None:
    labeled = _exact_targets(observation, "Home, tab 1 of 6")
    if len(labeled) == 1:
        return labeled[0]
    if len(labeled) > 1:
        return None
    menu = facebook_android.menu_target(observation)
    if menu is None:
        return None
    left, top, right, bottom = menu["bounds"]
    width = observation.screen[0]
    row = sorted((target for target in observation.targets
                  if target["bounds"][1] == top and target["bounds"][3] == bottom),
                 key=lambda target: target["bounds"][0])
    if len(row) != 6:
        return None
    step = width / 6
    if not all(abs(target["bounds"][0] - index * step) <= 3
               and abs(target["bounds"][2] - (index + 1) * step) <= 3
               for index, target in enumerate(row)):
        return None
    return row[0]


def _composer_open(observation: android.Observation) -> bool:
    return any(_norm(line) == "new post" for line in observation.read().splitlines())


def _picker_open(observation: android.Observation) -> bool:
    return any(_norm(line) == "add location" for line in observation.read().splitlines())


def _location_button(observation: android.Observation) -> dict[str, Any] | None:
    prefix = _norm("Location. 3 of 4. Press to add a location tag to your post")
    matches = [target for target in observation.targets
               if target.get("role") == "button"
               and _norm(target.get("label", "")).startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _has_current_location_tag(observation: android.Observation, place: str) -> bool:
    prefix = _norm("Location. 3 of 4. Current location tag added to your post: "
                   f"{place}. Press to edit or remove location tag")
    return any(_norm(target.get("label", "")).startswith(prefix)
               for target in observation.targets)


def _search_field(observation: android.Observation, query: str = "") -> dict[str, Any] | None:
    matches = [field for field in observation.fields
               if _norm(field.get("label", "")) in {"search", _norm(query)}
               or (_node_for_target(observation, field) is not None
                   and _norm(_node_for_target(observation, field).content_desc) == "search")]
    return matches[0] if len(matches) == 1 else None


def _matching_place_card(observation: android.Observation,
                         place: str) -> list[dict[str, Any]]:
    normalized_place = _norm(place)
    pattern = re.compile(
        rf"^{re.escape(normalized_place)},\s*{re.escape(normalized_place)},\s*"
        rf"[0-9][0-9,.]*\s*[kmb]?\s+check-ins,\s*"
        rf"{re.escape(normalized_place)}(?:,|$)", re.I)
    return [target for target in observation.targets
            if target.get("role") == "viewgroup"
            and "more actions" not in _norm(target.get("label", ""))
            and pattern.match(_norm(target.get("label", "")))]


def _require_account(actual: str | None, expected: str) -> None:
    if facebook_android.normalize(actual or "") != facebook_android.normalize(expected):
        raise Blocked("identity_mismatch", f"Facebook identifies {actual!r}, not requested account {expected!r}; no check-in was prepared.")


def _require_composer_account(observation: android.Observation, account: str) -> None:
    lines = {_norm(line) for line in observation.read().splitlines()}
    if _norm(account) not in lines:
        raise Blocked("identity_mismatch", "The composer does not identify the requested account; the check-in was not prepared.")


def _preview_target(observation: android.Observation, account: str,
                    place: str) -> list[dict[str, Any]]:
    prefix = _norm(f"{account} is at {place}.")
    return [target for target in observation.targets
            if target.get("role") == "button"
            and _norm(target.get("label", "")).startswith(prefix)]


def _is_preview(observation: android.Observation,
                workflow: facebook_android.Workflow, *, account: str,
                place: str) -> bool:
    lines = {_norm(line) for line in observation.read().splitlines()}
    return ("new post" in lines
            and bool(_preview_target(observation, account, place)))


def _audience_control(observation: android.Observation) -> list[dict[str, Any]]:
    return [target for target in observation.targets
            if target.get("role") == "button"
            and _norm(target.get("label", "")).startswith("post audience, ")]


def _require_public_audience(workflow: facebook_android.Workflow,
                             observation: android.Observation, *, account: str,
                             place: str) -> android.Observation:
    audience = _audience_control(observation)
    if len(audience) != 1:
        raise Blocked("unsupported_ui", f"Expected one observed Post audience control; found {len(audience)}.")
    if _norm(audience[0]["label"]) != "post audience, public":
        _tap(workflow, audience[0], "Post audience control")
        picker = workflow.wait(
            lambda obs: any(_norm(line) == "who can see your post?"
                            for line in obs.read().splitlines()),
            "The post audience picker did not open.")
        observation = _finish_audience_picker(workflow, picker, account=account, place=place)
    if len(_audience_control(observation)) != 1 or _norm(_audience_control(observation)[0]["label"]) != "post audience, public":
        raise Blocked("audience_unverified", "The preview does not show Post audience, Public.")
    return observation


def _finish_audience_picker(workflow: facebook_android.Workflow,
                            picker: android.Observation, *, account: str,
                            place: str) -> android.Observation:
    public = _exact_targets(picker, "Public, Anyone on or off Facebook", role="radio")
    if len(public) != 1:
        raise Blocked("unsupported_ui", f"Expected one exact Public audience option; found {len(public)}.")
    if not _node_checked(picker, public[0]):
        _tap(workflow, public[0], "Public audience option")
        picker = workflow.wait(
            lambda obs: any(_node_checked(obs, target) for target in
                            _exact_targets(obs, "Public, Anyone on or off Facebook", role="radio")),
            "Facebook did not confirm Public as the selected audience.")
    done = _exact_targets(picker, "Done", role="button")
    if len(done) != 1:
        raise Blocked("unsupported_ui", f"Expected one Done button in the audience picker; found {len(done)}.")
    _tap(workflow, done[0], "audience Done")
    return workflow.wait(
        lambda obs: _is_preview(obs, workflow, account=account, place=place)
        and len(_audience_control(obs)) == 1
        and _norm(_audience_control(obs)[0]["label"]) == "post audience, public",
        "The preview did not confirm the Public audience.")


def _node_checked(observation: android.Observation,
                  target: dict[str, Any]) -> bool:
    node = _node_for_target(observation, target)
    return bool(node and node.checked)


def _node_for_target(observation: android.Observation,
                     target: dict[str, Any]) -> android.Node | None:
    """Resolve a candidate id back to its source accessibility node.

    ``Observation.by_id`` intentionally returns the candidate dictionary, not a
    Node, so text and checked state must be read from ``observation.nodes``.
    """
    try:
        ref = int(target.get("id", ""))
    except (TypeError, ValueError):
        return None
    return next((node for node in observation.nodes if node.ref == ref), None)


def _verify_post_button(observation: android.Observation) -> None:
    post = _exact_targets(observation, "Post", role="button")
    if len(post) != 1:
        raise Blocked("unsupported_ui", f"Expected one enabled Post button in the verified preview; found {len(post)}.")


def _save_ready_draft(token: str, workflow: facebook_android.Workflow,
                      observation: android.Observation, *, account: str,
                      place: str, audience: str) -> dict[str, Any]:
    if not _is_preview(observation, workflow, account=account, place=place):
        raise Blocked("unsupported_ui", "The current screen is not a verified post preview.")
    if len(_audience_control(observation)) != 1 or _norm(_audience_control(observation)[0]["label"]) != _norm(f"Post audience, {audience}"):
        raise Blocked("audience_unverified", f"The preview does not show Post audience, {audience}.")
    _verify_post_button(observation)
    existing = facebook_flow_state.load(token)
    if existing["stage"] == "pre-submit":
        return existing
    return facebook_flow_state.save(
        token, "pre-submit", expected_stage="composing",
        evidence={"draft_ready": True, "draft_account": account,
                  "draft_place": place, "draft_audience": audience})


def _published_card(observation: android.Observation, *, account: str,
                    place: str, audience: str) -> list[dict[str, Any]]:
    """Require author, exact place, recency and visibility in the same feed card."""
    if _composer_open(observation):
        return []
    prefix = _norm(f"{account} is in {place}., Just now")
    return [target for target in observation.targets
            if target.get("role") == "viewgroup"
            and _norm(target.get("label", "")).startswith(prefix)
            and _norm(f"Shared with: {audience}") in _norm(target.get("label", ""))]


def confirm_post(workflow: facebook_android.Workflow, *, token: str) -> dict[str, Any]:
    """Validate the live preview, journal the attempt, then publish exactly once."""
    checkpoint = facebook_flow_state.load(token)
    if checkpoint["stage"] != "pre-submit":
        raise Blocked("requires_inspection", "Post may already have been submitted; it was not submitted again.")
    observation = workflow.observe()
    if not _is_preview(observation, workflow, account=checkpoint["account"], place=checkpoint["place"]):
        raise Blocked("draft_unverified", "The requested account and place are not shown in the post preview.")
    audiences = _audience_control(observation)
    if len(audiences) != 1 or _norm(audiences[0]["label"]) != _norm(f"Post audience, {checkpoint['audience']}"):
        raise Blocked("audience_unverified", "The preview does not show the requested audience; no post was submitted.")
    _verify_post_button(observation)
    workflow.ensure_time()
    post = _exact_targets(observation, "Post", role="button")[0]
    facebook_flow_state.save(token, "submitting", expected_stage="pre-submit")
    _tap(workflow, post, "verified Post button")
    published = workflow.wait(
        lambda obs: len(_published_card(obs, account=checkpoint["account"],
                                       place=checkpoint["place"], audience=checkpoint["audience"])) == 1,
        "The new Public check-in was not confirmed; the Post button must not be tapped again.")
    card = _published_card(published, account=checkpoint["account"],
                           place=checkpoint["place"], audience=checkpoint["audience"])[0]
    return facebook_flow_state.save(token, "posted", expected_stage="submitting",
                                    evidence={"post_verified": True, "post_signature": card["label"]})


def logout(workflow: facebook_android.Workflow, *, token: str) -> dict[str, Any]:
    """Sign out through the observed Menu and confirm the saved-account landing."""
    checkpoint = facebook_flow_state.load(token)
    if checkpoint["stage"] not in ("posted", "logging_out") or not checkpoint["evidence"].get("post_verified"):
        raise Blocked("post_unverified", "Logout requires a confirmed post in this workflow.")
    observation = workflow.observe(allow_saved_landing=True)
    if checkpoint["stage"] == "logging_out":
        if facebook_android.is_saved_login_landing(observation):
            return facebook_flow_state.save(token, "logged_out", expected_stage="logging_out",
                                            evidence={"logout_verified": True})
        # A visible confirmation is an incomplete logout. Never replay the
        # earlier Menu action or log out a different active account on resume.
        if "Log out of your account?" not in observation.read():
            raise Blocked("requires_inspection", "Logout was attempted but is not confirmed; the Log out action was not repeated.")
    else:
        observation = workflow.menu(observation)
        _require_account(facebook_android.identity(observation), checkpoint["account"])
        for _ in range(4):
            controls = _exact_targets(observation, "Log out", role="button")
            if len(controls) == 1:
                break
            if len(controls) > 1:
                raise Blocked("unsupported_ui", "Multiple Log out controls are visible.")
            workflow.ensure_time()
            android.scroll(workflow.serial, observation.screen, "scroll_down")
            observation = workflow.observe()
        else:
            raise Blocked("unsupported_ui", "The Facebook Menu did not expose a unique Log out button.")
        facebook_flow_state.save(token, "logging_out", expected_stage="posted")
        _tap(workflow, controls[0], "Menu Log out")
        while True:
            observation = workflow.observe(allow_saved_landing=True)
            if ("Log out of your account?" in observation.read()
                    or facebook_android.is_saved_login_landing(observation)):
                break
    if not facebook_android.is_saved_login_landing(observation):
        confirms = _exact_targets(observation, "LOG OUT", role="button")
        if len(confirms) != 1:
            raise Blocked("unsupported_ui", "The logout confirmation has no unique LOG OUT button.")
        _tap(workflow, confirms[0], "logout confirmation")
        # This wait must allow a saved-account landing instead of treating it
        # as a missing session blocker.
        while True:
            observation = workflow.observe(allow_saved_landing=True)
            if facebook_android.is_saved_login_landing(observation):
                break
    return facebook_flow_state.save(token, "logged_out", expected_stage="logging_out",
                                    evidence={"logout_verified": True})


def _result(state: dict[str, Any], *, state_name: str, started: float,
            detail: str | None = None, **extra: Any) -> dict[str, Any]:
    result = {"state": state_name, "stage": state["stage"],
              "resume_token": state["token"], "device": state["serial"],
              "account": state["account"], "place": state["place"],
              "audience": state["audience"], "evidence": state["evidence"],
              "complete": state["stage"] == "logged_out",
              "primary_location": state["evidence"].get("primary_location"),
              "primary_location_state": state["evidence"].get("primary_location_state"),
              "post_verified": state["evidence"].get("post_verified", False),
              "logout_verified": state["evidence"].get("logout_verified", False),
              "seconds": round(time.monotonic() - started, 3)}
    if detail:
        result["detail"] = detail
    result.update(extra)
    return result


def _identity_step(workflow: facebook_android.Workflow, account: str,
                   checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Establish requested identity once; a resumed login never reselects."""
    stage = checkpoint["stage"]
    if stage == "created":
        observed = workflow.observe(allow_saved_landing=True)
        checkpoint = facebook_flow_state.save(
            checkpoint["token"], "logging_in", expected_stage="created")
        if facebook_android.is_saved_login_landing(observed):
            actual, _ = workflow.bootstrap_saved_card(account, observation=observed)
        else:
            actual = workflow.select(account, observation=observed)
    elif stage == "logging_in":
        observed = workflow.observe(allow_saved_landing=True)
        observed = workflow.wait_pending_login(account, observed)
        observed = workflow.menu(observed)
        actual = facebook_android.identity(observed)
        if facebook_android.normalize(actual or "") != facebook_android.normalize(account):
            raise Blocked("identity_mismatch", f"Facebook Menu identifies {actual!r}, not requested account {account!r}; no location was read.")
    else:
        raise Blocked("unsupported_stage", f"Identity cannot be established from checkpoint stage {stage!r}.")

    if facebook_android.normalize(actual or "") != facebook_android.normalize(account):
        raise Blocked("identity_mismatch", f"Facebook identifies {actual!r}, not requested account {account!r}.")
    return facebook_flow_state.save(
        checkpoint["token"], "identity_verified", expected_stage="logging_in",
        evidence={"identity_verified": True, "verified_account": str(actual)})


def run(serial: str, account: str, place: str = "Manila, Philippines",
        audience: str = "Public", resume_token: str | None = None,
        timeout: float = 55, publish: bool = False,
        run_id: str | None = None) -> dict[str, Any]:
    """Run/resume the deterministic login, primary-location, and check-in flow.

    Publishing is opt-in per call. Checkpoint binding must exactly match the
    requested device, account, place, and audience. The irreversible submit
    stage is never automatically repeated after interruption.
    """
    started = time.monotonic()
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("device serial is required")
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account is required")
    if not isinstance(place, str) or not place.strip():
        raise ValueError("place is required")
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError("audience is required")
    if audience != "Public":
        raise ValueError("Only the verified Public check-in flow is supported")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ValueError("timeout must be a number from 1 to 60 seconds")
    if not isinstance(publish, bool):
        raise ValueError("publish must be a boolean")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200):
        raise ValueError("run_id must be a nonempty string of at most 200 characters")
    if publish and not (run_id or resume_token):
        raise ValueError("Publishing requires a stable run_id or resume_token")

    checkpoint = (facebook_flow_state.create(serial=serial, account=account,
                                             place=place, audience=audience, run_id=run_id)
                  if resume_token is None else facebook_flow_state.load(resume_token))
    requested = {"serial": serial, "account": account, "place": place,
                 "audience": audience}
    if any(checkpoint[field] != value for field, value in requested.items()):
        raise ValueError("resume token is bound to a different device, account, place, or audience")

    if checkpoint["stage"] in ("logged_out", "submitting"):
        note = {
            "logged_out": "This flow is already logged out; no phone action was repeated.",
            "submitting": "Post submission may already have occurred. Inspect Facebook before continuing; the Publish action was not repeated.",
            "logging_out": "Logout may already have occurred. Inspect Facebook before continuing; logout was not repeated.",
        }[checkpoint["stage"]]
        state_name = "complete" if checkpoint["stage"] == "logged_out" else "requires_inspection"
        return _result(checkpoint, state_name=state_name, started=started, detail=note)

    remaining = max(0.0, timeout - (time.monotonic() - started))
    workflow = facebook_android.Workflow(serial, timeout=remaining)
    workflow.deadline = started + timeout
    try:
        if checkpoint["stage"] in ("created", "logging_in"):
            checkpoint = _identity_step(workflow, account, checkpoint)

        if checkpoint["stage"] == "identity_verified":
            remaining = max(0.0, workflow.deadline - time.monotonic())
            if remaining < 12:
                return _result(checkpoint, state_name="timeout", started=started,
                               detail="Login is verified; resume this token with a fresh budget for the primary-location check.")
            location_result = facebook_android.run(
                serial, action="current_location", account=account,
                timeout=remaining, deadline=workflow.deadline)
            evidence: dict[str, Any] = {
                "primary_location_state": location_result.get("state", "unknown"),
                "primary_location_seconds": location_result.get("seconds"),
                "identity_verified": location_result.get("identity_verified") is True,
            }
            location = location_result.get("location")
            if isinstance(location, str) and location:
                evidence["primary_location"] = location
            checkpoint = facebook_flow_state.save(
                checkpoint["token"], "location_checked",
                expected_stage="identity_verified", evidence=evidence)
            if location_result.get("state") != "location" and not publish:
                return _result(checkpoint, state_name="primary_location_unavailable",
                               started=started,
                               detail=location_result.get("detail", "Primary location was not observed."),
                               timings=location_result.get("timings", {}))

        if checkpoint["stage"] == "location_checked" and not publish:
            return _result(checkpoint, state_name="location_checked", started=started,
                           detail="Primary location was checked. Publishing requires publish=true.")

        if checkpoint["stage"] == "location_checked" and publish:
            checkpoint = facebook_flow_state.save(
                checkpoint["token"], "composing", expected_stage="location_checked",
                evidence={"publish_enabled": True})
        publication_authorized = (publish or
                                  checkpoint["evidence"].get("publish_enabled") is True)
        if checkpoint["stage"] in ("composing", "pre-submit") and not publication_authorized:
            return _result(checkpoint, state_name="awaiting_publish_authorization",
                           started=started,
                           detail="This checkpoint has no recorded publish authorization.")

        if checkpoint["stage"] in ("composing", "pre-submit"):
            checkpoint = prepare_checkin(workflow, token=checkpoint["token"],
                                         place=place, audience=audience)
        if checkpoint["stage"] == "pre-submit":
            checkpoint = confirm_post(workflow, token=checkpoint["token"])
        if checkpoint["stage"] in ("posted", "logging_out"):
            checkpoint = logout(workflow, token=checkpoint["token"])
    except Blocked as exc:
        checkpoint = facebook_flow_state.load(checkpoint["token"])
        return _result(checkpoint, state_name=exc.state, started=started,
                       detail=exc.detail, timings=workflow.timings)
    except android.AdbError as exc:
        checkpoint = facebook_flow_state.load(checkpoint["token"])
        return _result(checkpoint, state_name="device_error", started=started,
                       detail=str(exc), timings=workflow.timings)

    checkpoint = facebook_flow_state.load(checkpoint["token"])
    return _result(checkpoint, state_name="complete" if checkpoint["stage"] == "logged_out" else checkpoint["stage"], started=started,
                   timings=workflow.timings)
