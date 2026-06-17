from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import anthropic

from slack_digest.config import Theme, get_config, save_config
from slack_digest.feedback import (
    expire_stale_proposals,
    get_last_tuning_run_date,
    get_unprocessed_votes,
    has_pending_proposal,
    insert_tuning_log,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_run_lock = threading.Lock()

TUNER_SYSTEM_PROMPT = """\
You are a digest tuning assistant. You analyze user feedback on a Slack digest \
to suggest improvements to the theme configuration and scoring parameters.

The user receives a daily digest of Slack messages filtered by themes. Each \
theme has a name, keywords (used for semantic similarity matching), a priority \
level, and an optional similarity threshold.

IMPORTANT — themes are inclusion-only. A message's score is the MAX over all \
themes of (similarity x priority_multiplier), and it is shown only if that score \
clears a threshold. Priority "low" still uses a positive multiplier (0.75) — it \
does NOT suppress. There is no negative or suppressing theme. Therefore adding a \
theme can only ever SURFACE MORE content matching it — never less. Adding a \
low-priority theme to "capture" or "deprioritize" disliked content does the \
OPPOSITE of what you intend: it makes that content match a theme and appear in \
the digest.

To reduce unwanted content, you must instead:
- remove the keywords that are catching it from the over-broad existing theme(s), and/or
- raise that theme's similarity_threshold, and/or
- lower the priority of an existing theme that keeps matching unwanted items.
NEVER use action "add" to suppress, capture, or deprioritize unwanted content. \
Only "add" a theme when the user clearly wants MORE of a topic that no current \
theme covers.

You will receive:
- The current theme configuration
- The global similarity threshold
- Items the user liked (thumbs up) and disliked (thumbs down)

Analyze the patterns in liked vs disliked items and suggest changes.

Return ONLY a JSON object with this structure:
{
  "theme_changes": [
    {
      "action": "update|add|remove",
      "name": "Theme Name",
      "keywords": ["updated", "keyword", "list"],
      "priority": "critical|high|medium|low",
      "reason": "Why this change"
    }
  ],
  "scoring_changes": [
    {
      "parameter": "similarity_threshold",
      "theme": "Theme Name or null for global",
      "old_value": 0.25,
      "new_value": 0.30,
      "reason": "Why this change"
    }
  ],
  "observations": "Free-text summary of what the user seems to care about"
}

Rules:
- Only suggest changes supported by the feedback data
- For priority changes, use theme_changes with action "update"
- similarity_threshold values should be between 0.1 and 0.5
- If no changes are needed for a category, return an empty array
- Be conservative — small adjustments are better than big rewrites
"""


def _build_tuning_prompt(votes: list, config) -> str:
    lines = ["Current themes:"]
    for t in config.themes:
        threshold_note = f", threshold={t.similarity_threshold}" if t.similarity_threshold else ""
        lines.append(f"- {t.name} ({t.priority}): {', '.join(t.keywords)}{threshold_note}")

    lines.append(f"\nGlobal similarity threshold: {config.scoring.similarity_threshold}")

    up_items = [v for v in votes if v["vote"] == "up"]
    down_items = [v for v in votes if v["vote"] == "down"]

    lines.append(f"\nItems the user liked ({len(up_items)} thumbs up):")
    for v in up_items:
        themes_str = v["themes"] or "none"
        lines.append(f"  - [{v['section']}] {v['channel']}: {v['summary']} (themes={themes_str}, score={v['score']})")

    lines.append(f"\nItems the user disliked ({len(down_items)} thumbs down):")
    for v in down_items:
        themes_str = v["themes"] or "none"
        lines.append(f"  - [{v['section']}] {v['channel']}: {v['summary']} (themes={themes_str}, score={v['score']})")

    return "\n".join(lines)


def _parse_tuner_response(text: str) -> dict:
    import re
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?\s*```\s*$", "", text)
    text = text.strip()

    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return {"theme_changes": [], "scoring_changes": [], "observations": "Failed to parse tuner response"}


def _snapshot_config(config) -> dict:
    return {
        "themes": [t.model_dump(mode="json") for t in config.themes],
        "scoring": config.scoring.model_dump(mode="json"),
    }


def _apply_changes(changes: dict) -> None:
    config = get_config().model_copy(deep=True)

    for tc in changes.get("theme_changes", []):
        action = tc.get("action")
        name = tc.get("name", "")
        if action == "remove":
            config.themes = [t for t in config.themes if t.name != name]
        elif action == "add":
            config.themes = [t for t in config.themes if t.name != name]
            config.themes.append(Theme(
                name=name,
                keywords=tc.get("keywords", []),
                priority=tc.get("priority", "medium"),
                similarity_threshold=tc.get("similarity_threshold"),
            ))
        elif action == "update":
            for t in config.themes:
                if t.name == name:
                    if "keywords" in tc:
                        t.keywords = tc["keywords"]
                    if "priority" in tc:
                        t.priority = tc["priority"]
                    if "similarity_threshold" in tc:
                        t.similarity_threshold = tc["similarity_threshold"]
                    break

    for sc in changes.get("scoring_changes", []):
        if sc.get("parameter") == "similarity_threshold":
            theme_name = sc.get("theme")
            new_val = sc.get("new_value")
            if new_val is not None:
                if theme_name is None:
                    config.scoring.similarity_threshold = new_val
                else:
                    for t in config.themes:
                        if t.name == theme_name:
                            t.similarity_threshold = new_val
                            break

    save_config(config)
    logger.info("Tuning changes applied to config")


def _restore_config(snapshot: dict) -> None:
    config = get_config().model_copy(deep=True)
    config.themes = [Theme.model_validate(t) for t in snapshot.get("themes", [])]
    if "scoring" in snapshot:
        config.scoring.similarity_threshold = snapshot["scoring"].get("similarity_threshold", 0.25)
    save_config(config)
    logger.info("Config restored from snapshot")


def run_tuning(
    send_proposal: Callable | None = None,
    send_summary: Callable | None = None,
) -> dict | None:
    config = get_config()
    votes = get_unprocessed_votes()
    if not votes:
        logger.info("No unprocessed votes — skipping tuning")
        return None

    vote_dicts = [dict(v) for v in votes]
    up_count = sum(1 for v in vote_dicts if v["vote"] == "up")
    down_count = sum(1 for v in vote_dicts if v["vote"] == "down")

    config_before = _snapshot_config(config)
    prompt = _build_tuning_prompt(vote_dicts, config)

    logger.info(f"Running tuning analysis ({up_count} up, {down_count} down)...")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.agent.model,
        max_tokens=4096,
        system=TUNER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    changes = _parse_tuner_response(response.content[0].text)
    logger.info(f"Tuning analysis complete: {len(changes.get('theme_changes', []))} theme changes, "
                f"{len(changes.get('scoring_changes', []))} scoring changes")

    if config.tuning.auto_apply:
        _apply_changes(changes)
        log_id = insert_tuning_log(
            votes_up=up_count,
            votes_down=down_count,
            changes=changes,
            config_before=config_before,
            status="applied",
            applied_at=datetime.now(timezone.utc).isoformat(),
        )
        if send_summary:
            send_summary(changes, log_id, config_before)
    else:
        log_id = insert_tuning_log(
            votes_up=up_count,
            votes_down=down_count,
            changes=changes,
            config_before=config_before,
        )
        if send_proposal:
            send_proposal(changes, log_id, config_before)

    return changes


def maybe_run(
    send_proposal: Callable | None = None,
    send_summary: Callable | None = None,
) -> bool:
    if not _run_lock.acquire(blocking=False):
        return False
    try:
        config = get_config()
        tuning = config.tuning

        expire_stale_proposals(tuning.cooldown_days)
        if has_pending_proposal():
            return False

        votes = get_unprocessed_votes()
        if len(votes) < tuning.min_votes:
            return False

        last_run = get_last_tuning_run_date()
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_dt < timedelta(days=tuning.cooldown_days):
                return False

        vote_dicts = [dict(v) for v in votes]
        down_count = sum(1 for v in vote_dicts if v["vote"] == "down")
        if len(vote_dicts) > 0 and (down_count / len(vote_dicts)) <= tuning.pain_threshold:
            return False

        thread = threading.Thread(
            target=run_tuning,
            kwargs={"send_proposal": send_proposal, "send_summary": send_summary},
            daemon=True,
        )
        thread.start()
        return True
    finally:
        _run_lock.release()


def force_run(
    send_proposal: Callable | None = None,
    send_summary: Callable | None = None,
) -> bool:
    if has_pending_proposal():
        return False

    thread = threading.Thread(
        target=run_tuning,
        kwargs={"send_proposal": send_proposal, "send_summary": send_summary},
        daemon=True,
    )
    thread.start()
    return True
