from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING

from slack_bolt import App

from slack_digest import feedback as feedback_store
from slack_digest import tuner
from slack_digest.config import (
    DigestConfig,
    Person,
    Theme,
    add_person,
    add_theme,
    get_config,
    remove_person,
    remove_theme,
    save_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from slack_sdk import WebClient

    from slack_digest.scheduler import DigestScheduler

logger = logging.getLogger(__name__)


def create_app(
    config: DigestConfig,
    web_client: WebClient,
    scheduler: DigestScheduler,
    run_digest: Callable,
    reader_client: WebClient | None = None,
) -> App:
    app = App(token=web_client.token)

    def _make_send_proposal(target_user_id: str):
        def send_proposal(changes: dict, log_id: int, config_before: dict):
            blocks = _format_proposal_blocks(changes, log_id, config_before)
            web_client.chat_postMessage(
                channel=target_user_id,
                blocks=blocks,
                text=f"Theme tuning proposal: {changes.get('observations', 'Review suggested changes')}",
            )
        return send_proposal

    def _make_send_summary(target_user_id: str):
        def send_summary(changes: dict, log_id: int, config_before: dict):
            blocks = _format_summary_blocks(changes, log_id, config_before)
            web_client.chat_postMessage(
                channel=target_user_id,
                blocks=blocks,
                text=f"Theme tuning applied: {changes.get('observations', 'Changes applied')}",
            )
        return send_summary

    @app.action("feedback_up")
    def handle_feedback_up(ack, body, client):
        ack()
        _handle_feedback(body, client, "up")

    @app.action("feedback_down")
    def handle_feedback_down(ack, body, client):
        ack()
        _handle_feedback(body, client, "down")

    def _handle_feedback(body, client, vote: str):
        action = body["actions"][0]
        feedback_id = action["value"]

        current_vote = feedback_store.get_current_vote(feedback_id)
        if current_vote == vote:
            feedback_store.delete_feedback(feedback_id)
            active_vote = None
        else:
            if not feedback_store.record_feedback(feedback_id, vote):
                return
            active_vote = vote

        blocks = body["message"]["blocks"]
        target_block_id = f"feedback_actions_{feedback_id}"

        new_blocks = []
        for block in blocks:
            if block.get("block_id") == target_block_id:
                elements = _build_vote_buttons(feedback_id, active_vote)
                for el in block.get("elements", []):
                    if el.get("action_id", "").startswith("view_"):
                        elements.append(el)
                new_blocks.append({
                    "type": "actions",
                    "block_id": target_block_id,
                    "elements": elements,
                })
            else:
                new_blocks.append(block)

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        client.chat_update(channel=channel, ts=ts, blocks=new_blocks)

        if active_vote is not None:
            cfg = get_config()
            target_user_id = cfg.target_user.slack_id
            tuner.maybe_run(
                send_proposal=_make_send_proposal(target_user_id),
                send_summary=_make_send_summary(target_user_id),
            )

    def _build_vote_buttons(feedback_id: str, active_vote: str | None) -> list[dict]:
        up_btn = {
            "type": "button",
            "action_id": "feedback_up",
            "value": feedback_id,
        }
        down_btn = {
            "type": "button",
            "action_id": "feedback_down",
            "value": feedback_id,
        }
        if active_vote == "up":
            up_btn["text"] = {"type": "plain_text", "text": ":white_check_mark: Useful"}
            up_btn["style"] = "primary"
            down_btn["text"] = {"type": "plain_text", "text": ":-1:"}
        elif active_vote == "down":
            up_btn["text"] = {"type": "plain_text", "text": ":+1:"}
            down_btn["text"] = {"type": "plain_text", "text": ":x: Not useful"}
            down_btn["style"] = "danger"
        else:
            up_btn["text"] = {"type": "plain_text", "text": ":+1:"}
            down_btn["text"] = {"type": "plain_text", "text": ":-1:"}
        return [up_btn, down_btn]

    @app.action("tuning_apply")
    def handle_tuning_apply(ack, body, client):
        ack()
        log_id = int(body["actions"][0]["value"])
        changes = feedback_store.apply_tuning(log_id)
        if not changes:
            return
        tuner._apply_changes(changes)

        blocks = body["message"]["blocks"]
        new_blocks = []
        for block in blocks:
            if block.get("block_id") == "tuning_actions":
                new_blocks.append({
                    "type": "context",
                    "block_id": "tuning_actions",
                    "elements": [{"type": "mrkdwn", "text": ":white_check_mark: Changes applied"}],
                })
            else:
                new_blocks.append(block)
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=new_blocks)

    @app.action("tuning_dismiss")
    def handle_tuning_dismiss(ack, body, client):
        ack()
        log_id = int(body["actions"][0]["value"])
        if not feedback_store.dismiss_tuning(log_id):
            return

        blocks = body["message"]["blocks"]
        new_blocks = []
        for block in blocks:
            if block.get("block_id") == "tuning_actions":
                new_blocks.append({
                    "type": "context",
                    "block_id": "tuning_actions",
                    "elements": [{"type": "mrkdwn", "text": "Dismissed — votes will re-trigger after cooldown"}],
                })
            else:
                new_blocks.append(block)
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=new_blocks)

    @app.action(re.compile(".*"))
    def handle_other_actions(ack, body):
        ack()

    @app.command("/digest-config")
    def handle_config(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()

        if not text:
            cfg = get_config()
            lines = [
                f"*Schedule:* {', '.join(f'`{t}`' for t in cfg.digest.schedule)} ({cfg.digest.timezone})",
                f"*Lookback:* {cfg.digest.lookback_hours} hours",
                f"*Themes:* {len(cfg.themes)}",
                f"*People:* {len(cfg.people)}",
                f"*Excluded channels:* {', '.join(cfg.exclude_channels) or 'none'}",
                f"*Model:* `{cfg.agent.model}`",
                f"*Budget:* ${cfg.agent.max_budget_usd:.2f}/run",
            ]
            respond(text="\n".join(lines))
            return

        parts = text.split(maxsplit=1)
        sub = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""

        cfg = get_config().model_copy(deep=True)

        if sub == "schedule":
            if not re.match(r"^\d{2}:\d{2}$", value):
                respond(text="Usage: `/digest-config schedule HH:MM`")
                return
            cfg.digest.schedule = [value]
            save_config(cfg)
            scheduler.reschedule(cfg)
            respond(text=f"Digest schedule updated to `{value}`")

        elif sub == "lookback":
            try:
                hours = int(value)
                if hours < 1 or hours > 168:
                    raise ValueError
            except ValueError:
                respond(text="Usage: `/digest-config lookback <1-168>`")
                return
            cfg.digest.lookback_hours = hours
            save_config(cfg)
            respond(text=f"Lookback updated to {hours} hours")

        elif sub == "timezone":
            cfg.digest.timezone = value
            save_config(cfg)
            respond(text=f"Timezone updated to `{value}`")

        else:
            respond(text=f"Unknown setting `{sub}`. Options: `schedule`, `lookback`, `timezone`")

    @app.command("/digest-themes")
    def handle_themes(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()

        if not text:
            cfg = get_config()
            if not cfg.themes:
                respond(text="No themes configured. Use `/digest-themes add <name> <keywords> [priority]`")
                return
            lines = []
            for t in cfg.themes:
                lines.append(f"• *{t.name}* ({t.priority}) — {', '.join(t.keywords)}")
            respond(text="\n".join(lines))
            return

        parts = text.split(maxsplit=1)
        action = parts[0].lower()

        if action == "add" and len(parts) > 1:
            # Format: add <name> <keyword1,keyword2> [priority]
            match = re.match(r'^"([^"]+)"\s+(\S+)(?:\s+(critical|high|medium|low))?$', parts[1])
            if not match:
                match = re.match(r"^(\S+)\s+(\S+)(?:\s+(critical|high|medium|low))?$", parts[1])
            if not match:
                respond(text='Usage: `/digest-themes add "Theme Name" keyword1,keyword2 [high]`')
                return
            name = match.group(1)
            keywords = match.group(2).split(",")
            priority = match.group(3) or "medium"
            add_theme(Theme(name=name, keywords=keywords, priority=priority))
            respond(text=f"Added theme *{name}* ({priority}): {', '.join(keywords)}")

        elif action == "remove" and len(parts) > 1:
            name = parts[1].strip().strip('"')
            remove_theme(name)
            respond(text=f"Removed theme *{name}*")

        else:
            respond(text="Usage: `/digest-themes [add|remove] ...`")

    @app.command("/digest-people")
    def handle_people(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()

        if not text:
            cfg = get_config()
            if not cfg.people:
                respond(text="No people configured. Use `/digest-people add @username [reason]`")
                return
            lines = []
            for p in cfg.people:
                lines.append(f"• *{p.name}* (<@{p.slack_id}>) — {p.reason}")
            respond(text="\n".join(lines))
            return

        parts = text.split(maxsplit=1)
        action = parts[0].lower()

        if action == "add" and len(parts) > 1:
            match = re.match(r"^<@(\w+)(?:\|([^>]*))?>\s*(.*)?$", parts[1])
            if not match:
                respond(text="Usage: `/digest-people add @username [reason]`")
                return
            slack_id = match.group(1)
            name = match.group(2) or slack_id
            reason = (match.group(3) or "").strip().strip('"')
            add_person(Person(slack_id=slack_id, name=name, reason=reason))
            respond(text=f"Added <@{slack_id}> to digest tracking")

        elif action == "remove" and len(parts) > 1:
            match = re.match(r"^<@(\w+)(?:\|[^>]*)?>", parts[1])
            if not match:
                respond(text="Usage: `/digest-people remove @username`")
                return
            remove_person(match.group(1))
            respond(text=f"Removed <@{match.group(1)}> from digest tracking")

        else:
            respond(text="Usage: `/digest-people [add|remove] ...`")

    @app.command("/digest-now")
    def handle_digest_now(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip()

        if text:
            try:
                hours = int(text)
                if hours < 1 or hours > 168:
                    raise ValueError
            except ValueError:
                respond(text="Usage: `/digest-now [hours]` (1-168)")
                return
            respond(text=f"Generating digest for the last {hours} hours... This may take 1-2 minutes.")
        else:
            hours = None
            respond(text="Generating your digest now... This may take 1-2 minutes.")

        def _run():
            try:
                run_digest(fixed_lookback_hours=hours, triggered=True)
            except Exception:
                logger.exception("On-demand digest generation failed")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    @app.command("/digest-tune")
    def handle_tune(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip().lower()
        cfg = get_config()
        target_user_id = cfg.target_user.slack_id

        if text == "history":
            history = feedback_store.get_tuning_history()
            if not history:
                respond(text="No tuning runs yet.")
                return
            lines = []
            for h in history:
                obs = h["observations"][:60] + "..." if len(h["observations"]) > 60 else h["observations"]
                lines.append(
                    f"• `{h['run_date'][:10]}` — {h['status']} "
                    f"({h['votes_up']}↑ {h['votes_down']}↓) {obs}"
                )
            respond(text="\n".join(lines))
            return

        if text == "revert":
            snapshot = feedback_store.get_last_applied_config_before()
            if not snapshot:
                respond(text="No applied tuning runs to revert.")
                return
            tuner._restore_config(snapshot)
            respond(text="Reverted to config before last applied tuning run.")
            return

        if feedback_store.has_pending_proposal():
            respond(text="A tuning proposal is pending — apply or dismiss it first.")
            return

        started = tuner.force_run(
            send_proposal=_make_send_proposal(target_user_id),
            send_summary=_make_send_summary(target_user_id),
        )
        if started:
            respond(text="Running theme tuning analysis... Results will arrive as a DM.")
        else:
            respond(text="Could not start tuning — a proposal may be pending.")

    @app.command("/digest-rescan")
    def handle_rescan(ack, respond, command):
        ack()
        respond(text="Rescanning all channels... This may take a minute.")

        def _run():
            try:
                from slack_digest.main import scan_and_cache_channels, CHANNEL_CACHE_FILE
                import json
                scan_and_cache_channels(reader_client or web_client, get_config())
                total = len(json.loads(CHANNEL_CACHE_FILE.read_text())["channels"])
                respond(text=f"Done — scanned and cached {total} channels.")
            except Exception:
                logger.exception("Channel rescan failed")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    return app


def _theme_index(config_before: dict) -> dict[str, dict]:
    return {t["name"]: t for t in config_before.get("themes", [])}


def _format_proposal_blocks(changes: dict, log_id: int, config_before: dict) -> list[dict]:
    blocks: list[dict] = []
    current_themes = _theme_index(config_before)
    global_threshold = config_before.get("scoring", {}).get("similarity_threshold", 0.25)

    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Theme Tuning Proposal"}})

    obs = changes.get("observations", "")
    if obs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Observations:* {obs}"}})

    blocks.append({"type": "divider"})

    for tc in changes.get("theme_changes", []):
        action = tc.get("action", "update")
        name = tc.get("name", "")
        emoji = {"add": ":heavy_plus_sign:", "remove": ":x:", "update": ":pencil2:"}.get(action, ":pencil2:")
        text = f"{emoji} *{action.title()}* theme *{name}*"

        if action == "remove":
            old = current_themes.get(name)
            if old:
                text += f"\n  _Currently:_ {old.get('priority', 'medium')} — {', '.join(old.get('keywords', []))}"
        elif action == "update":
            old = current_themes.get(name)
            if old:
                old_kw = ', '.join(old.get('keywords', []))
                new_kw = ', '.join(tc.get('keywords', old.get('keywords', [])))
                old_pri = old.get('priority', 'medium')
                new_pri = tc.get('priority', old_pri)
                text += f"\n  *Keywords:* `{old_kw}` → `{new_kw}`" if old_kw != new_kw else f"\n  *Keywords:* {new_kw}"
                text += f"\n  *Priority:* `{old_pri}` → `{new_pri}`" if old_pri != new_pri else f"\n  *Priority:* {new_pri}"
            else:
                text += f"\n  *Keywords:* {', '.join(tc.get('keywords', []))}"
                text += f"\n  *Priority:* {tc.get('priority', 'medium')}"
        elif action == "add":
            text += f"\n  *Keywords:* {', '.join(tc.get('keywords', []))}"
            text += f"\n  *Priority:* {tc.get('priority', 'medium')}"

        text += f"\n  _Reason: {tc.get('reason', '')}_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    for sc in changes.get("scoring_changes", []):
        theme = sc.get("theme") or "global"
        text = (
            f":chart_with_upwards_trend: *Scoring* — {theme} similarity_threshold: "
            f"`{sc.get('old_value', global_threshold)}` → `{sc.get('new_value')}`"
            f"\n  _Reason: {sc.get('reason', '')}_"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    if not changes.get("theme_changes") and not changes.get("scoring_changes"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No changes suggested."}})

    # Show current config for full context
    if current_themes:
        lines = ["*Current themes for reference:*"]
        for name, t in current_themes.items():
            thr = t.get("similarity_threshold")
            thr_str = f", threshold={thr}" if thr else ""
            lines.append(f"  • {name} ({t.get('priority', 'medium')}{thr_str}): {', '.join(t.get('keywords', []))}")
        lines.append(f"  _Global threshold: {global_threshold}_")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(lines)}]})

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "block_id": "tuning_actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Apply"},
                "style": "primary",
                "action_id": "tuning_apply",
                "value": str(log_id),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Dismiss"},
                "action_id": "tuning_dismiss",
                "value": str(log_id),
            },
        ],
    })

    return blocks


def _format_summary_blocks(changes: dict, log_id: int, config_before: dict) -> list[dict]:
    blocks: list[dict] = []
    current_themes = _theme_index(config_before)
    global_threshold = config_before.get("scoring", {}).get("similarity_threshold", 0.25)

    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Theme Tuning Applied"}})

    obs = changes.get("observations", "")
    if obs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Observations:* {obs}"}})

    lines = []
    for tc in changes.get("theme_changes", []):
        action = tc.get("action", "update")
        name = tc.get("name", "")
        old = current_themes.get(name)

        if action == "update" and old:
            old_kw = ', '.join(old.get('keywords', []))
            new_kw = ', '.join(tc.get('keywords', old.get('keywords', [])))
            old_pri = old.get('priority', 'medium')
            new_pri = tc.get('priority', old_pri)
            parts = [f"*{name}*"]
            if old_kw != new_kw:
                parts.append(f"keywords: `{old_kw}` → `{new_kw}`")
            if old_pri != new_pri:
                parts.append(f"priority: `{old_pri}` → `{new_pri}`")
            lines.append(f"• Updated {', '.join(parts)}")
        elif action == "add":
            lines.append(f"• Added *{name}* ({tc.get('priority', 'medium')}): {', '.join(tc.get('keywords', []))}")
        elif action == "remove":
            old_desc = ""
            if old:
                old_desc = f" (was: {old.get('priority', 'medium')} — {', '.join(old.get('keywords', []))})"
            lines.append(f"• Removed *{name}*{old_desc}")

    for sc in changes.get("scoring_changes", []):
        theme = sc.get("theme") or "global"
        lines.append(f"• {theme} threshold: `{sc.get('old_value', global_threshold)}` → `{sc.get('new_value')}`")

    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No changes were needed."}})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Use `/digest-tune revert` to undo (run #{log_id})"}],
    })

    return blocks
