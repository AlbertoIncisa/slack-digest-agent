from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import anthropic

from slack_digest.config import DigestConfig
from slack_digest.scorer import ScoredMessage

logger = logging.getLogger(__name__)

MAX_BLOCKS = 50

SYSTEM_PROMPT = """\
You are a Slack Digest Agent. You receive pre-filtered, scored messages from Slack \
and synthesize them into a concise daily digest for {user_name}.

Each message has a relevance score and matched themes — use these to prioritize.

## Output Format
Return a JSON object with this structure:
```json
{{
  "sections": [
    {{
      "title": "Section Title",
      "priority": "critical|high|medium|low",
      "items": [
        {{
          "channel": "#channel-name",
          "summary": "Brief 1-2 sentence summary",
          "author": "Person Name",
          "relevance": "Why this matters",
          "message_id": "msg_0"
        }}
      ]
    }}
  ],
  "stats": {{
    "channels_scanned": 42,
    "messages_processed": 1200,
    "messages_highlighted": 65,
    "period": "Last 24 hours"
  }},
  "one_liner": "Single sentence capturing the most important thing today"
}}
```

## Rules
- Group related messages into thematic sections
- Critical/high priority sections come first
- Each item summary: 1-2 sentences max
- Include #channel-name for navigation
- Merge related messages from the same discussion into one item
- For each item, set message_id to the ID of the most relevant source message (e.g. "msg_0")
- If thread replies are included, synthesize them — don't list each reply
- Omit themes with nothing notable
- Your response MUST be only the JSON object, nothing else
"""


def _format_messages_for_prompt(scored_messages: list[ScoredMessage], stats: dict) -> str:
    lines = [
        f"Messages from the {stats.get('period', 'last 24 hours')}:",
        f"Total channels scanned: {stats['channels_scanned']}",
        f"Total messages processed: {stats['messages_processed']}",
        f"Messages selected as relevant: {len(scored_messages)}",
        "",
    ]

    for idx, msg in enumerate(scored_messages):
        themes = ", ".join(f"{name} ({score:.2f})" for name, score in msg.matched_themes) if msg.matched_themes else "general"
        lines.append(f"--- msg_{idx} [#{msg.channel_name}] score={msg.score:.2f} themes=[{themes}]")
        lines.append(f"Author: {msg.author_name}")
        lines.append(f"Replies: {msg.reply_count}, Reactions: {sum(r.get('count', 0) for r in msg.reactions)}")
        lines.append(msg.text)
        if msg.thread_replies:
            lines.append(f"  Thread ({len(msg.thread_replies)} replies):")
            for reply in msg.thread_replies[:5]:
                lines.append(f"    {reply.get('author_name', reply.get('user', '?'))}: {reply['text'][:200]}")
        lines.append("")

    return "\n".join(lines)


def _build_permalink(workspace_url: str, channel_id: str, ts: str) -> str:
    ts_clean = ts.replace(".", "")
    base = workspace_url.rstrip("/")
    return f"{base}/archives/{channel_id}/p{ts_clean}"


def generate_digest(
    scored_messages: list[ScoredMessage],
    config: DigestConfig,
    stats: dict,
    workspace_url: str = "https://lovable-dev.slack.com",
) -> dict:
    user_name = config.target_user.email or "the user"
    system_prompt = SYSTEM_PROMPT.format(user_name=user_name)
    user_prompt = _format_messages_for_prompt(scored_messages, stats)

    message_links = {}
    source_by_message_id = {}
    for idx, msg in enumerate(scored_messages):
        key = f"msg_{idx}"
        message_links[key] = _build_permalink(workspace_url, msg.channel_id, msg.ts)
        source_by_message_id[key] = {
            "themes": [{"name": name, "score": score} for name, score in msg.matched_themes],
            "score": msg.score,
            "raw_text": msg.text,
            "channel_id": msg.channel_id,
            "ts": msg.ts,
        }

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.agent.model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    result_text = response.content[0].text
    logger.info(f"Digest generated — {len(result_text)} chars")
    digest = _parse_digest_json(result_text)
    digest["_message_links"] = message_links
    digest["_source_by_message_id"] = source_by_message_id
    return digest


def _parse_digest_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?\s*```\s*$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

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

    return {
        "sections": [
            {
                "title": "Digest",
                "priority": "medium",
                "items": [{"channel": "#general", "summary": text[:500], "author": "", "relevance": "JSON parsing failed"}],
            }
        ],
        "stats": {"channels_scanned": 0, "messages_processed": 0, "period": "Unknown"},
        "one_liner": "Digest generated but formatting failed",
    }


BLOCKS_PER_MESSAGE = 50
OVERHEAD_BLOCKS = 4
ITEMS_PER_MESSAGE = (BLOCKS_PER_MESSAGE - OVERHEAD_BLOCKS) // 2


def format_digest_blocks(digest: dict, digest_run_id: str, label: str = "Daily Digest") -> list[list[dict]]:
    from slack_digest.feedback import save_digest_item

    message_links = digest.pop("_message_links", {})
    source_map = digest.pop("_source_by_message_id", {})
    today_str = datetime.now(timezone.utc).strftime("%A, %B %d")
    digest_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    priority_indicator = {
        "critical": ":red_circle:",
        "high": ":large_orange_circle:",
        "medium": ":large_yellow_circle:",
        "low": ":white_circle:",
    }

    # Each item_group is a list of blocks that must stay in the same message
    # (section text + actions buttons, or a section header, or a divider).
    item_groups: list[list[dict]] = []

    for section_idx, section in enumerate(digest.get("sections", [])):
        indicator = priority_indicator.get(section.get("priority", "medium"), ":white_circle:")
        item_groups.append([{"type": "section", "text": {"type": "mrkdwn", "text": f"{indicator} *{section['title']}*"}}])

        for item_idx, item in enumerate(section.get("items", [])):
            feedback_id = f"{digest_run_id}_{section_idx}_{item_idx}"
            msg_id = item.get("message_id", "")
            link = message_links.get(msg_id)
            source = source_map.get(msg_id, {})

            save_digest_item(
                feedback_id=feedback_id,
                digest_run_id=digest_run_id,
                digest_date=digest_date,
                section=section.get("title", ""),
                channel=item.get("channel", ""),
                author=item.get("author", ""),
                summary=item.get("summary", ""),
                relevance=item.get("relevance"),
                themes=source.get("themes"),
                score=source.get("score"),
                raw_text=source.get("raw_text"),
            )

            author_part = f" — _{item['author']}_" if item.get("author") else ""
            relevance_part = f"\n>{item['relevance']}" if item.get("relevance") else ""
            text = f"*{item.get('channel', '')}*{author_part}\n{item.get('summary', '')}{relevance_part}"
            section_block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}

            actions_elements = [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":+1:"},
                    "action_id": "feedback_up",
                    "value": feedback_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":-1:"},
                    "action_id": "feedback_down",
                    "value": feedback_id,
                },
            ]
            if link:
                actions_elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View"},
                    "url": link,
                    "action_id": f"view_{feedback_id}",
                })
            actions_block = {
                "type": "actions",
                "block_id": f"feedback_actions_{feedback_id}",
                "elements": actions_elements,
            }

            item_groups.append([section_block, actions_block])

        item_groups.append([{"type": "divider"}])

    stats = digest.get("stats", {})
    footer = {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Scanned {stats.get('channels_scanned', '?')} channels, "
                    f"{stats.get('messages_processed', '?')} messages | "
                    f"{stats.get('period', 'N/A')}"
                ),
            }
        ],
    }

    one_liner = digest.get("one_liner", "")
    max_body_blocks = BLOCKS_PER_MESSAGE - OVERHEAD_BLOCKS

    chunks: list[list[list[dict]]] = []
    current_chunk: list[list[dict]] = []
    current_count = 0
    for group in item_groups:
        group_size = len(group)
        if current_count + group_size > max_body_blocks and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_count = 0
        current_chunk.append(group)
        current_count += group_size
    if current_chunk:
        chunks.append(current_chunk)

    if not chunks:
        chunks = [[]]

    all_messages: list[list[dict]] = []
    total = len(chunks)

    for page_idx, chunk in enumerate(chunks):
        blocks: list[dict] = []
        if page_idx == 0:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": f"{label} — {today_str}"}})
            if one_liner:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*TL;DR:* {one_liner}"}})
            blocks.append({"type": "divider"})
        elif total > 1:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": f"{label} ({page_idx + 1}/{total}) — {today_str}"},
            })
            blocks.append({"type": "divider"})

        for group in chunk:
            blocks.extend(group)

        if page_idx == total - 1:
            blocks.append(footer)

        all_messages.append(blocks)

    return all_messages
