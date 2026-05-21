from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING

from slack_bolt import App

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
) -> App:
    app = App(token=web_client.token)

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
            cfg.digest.schedule = value
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
                respond(text="No people configured. Use `/digest-people add <@user> <reason>`")
                return
            lines = []
            for p in cfg.people:
                lines.append(f"• *{p.name}* (<@{p.slack_id}>) — {p.reason}")
            respond(text="\n".join(lines))
            return

        parts = text.split(maxsplit=1)
        action = parts[0].lower()

        if action == "add" and len(parts) > 1:
            # Format: add <@U01ABC123|name> "reason"
            match = re.match(r"^<@(\w+)(?:\|([^>]*))?>\s*(.*)?$", parts[1])
            if not match:
                respond(text="Usage: `/digest-people add @username reason for tracking`")
                return
            slack_id = match.group(1)
            name = match.group(2) or slack_id
            reason = match.group(3) or ""
            add_person(Person(slack_id=slack_id, name=name, reason=reason.strip('"').strip()))
            respond(text=f"Added <@{slack_id}> to digest tracking")

        elif action == "remove" and len(parts) > 1:
            match = re.match(r"^<@(\w+)(?:\|[^>]*)?>", parts[1])
            if not match:
                respond(text="Usage: `/digest-people remove @username`")
                return
            slack_id = match.group(1)
            remove_person(slack_id)
            respond(text=f"Removed <@{slack_id}> from digest tracking")

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
                run_digest(fixed_lookback_hours=hours)
            except Exception:
                logger.exception("On-demand digest generation failed")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    return app
