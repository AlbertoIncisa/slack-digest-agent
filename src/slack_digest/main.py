from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta

from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from slack_digest.agent import format_digest_blocks, generate_digest
from slack_digest.bot import create_app
from slack_digest.config import DigestConfig, load_config, save_config
from slack_digest.scheduler import DigestScheduler, _load_last_run
from slack_digest.scorer import MessageScorer
from slack_digest.slack_client import (
    get_all_public_channels,
    get_channel_messages,
    get_thread_replies,
    get_user_info,
    lookup_user_by_email,
    send_dm,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("slack-digest")



def _validate_env() -> None:
    required = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN", "ANTHROPIC_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def _resolve_target_user(config: DigestConfig, client: WebClient) -> DigestConfig:
    if config.target_user.slack_id:
        return config
    if not config.target_user.email:
        logger.error("No target_user.slack_id or target_user.email in config.")
        sys.exit(1)
    slack_id = lookup_user_by_email(client, config.target_user.email)
    if not slack_id:
        logger.error(f"Could not find Slack user with email: {config.target_user.email}")
        sys.exit(1)
    config.target_user.slack_id = slack_id
    save_config(config)
    logger.info(f"Resolved target user {config.target_user.email} -> {slack_id}")
    return config


MAX_CHANNELS = 100
CHANNEL_RELEVANCE_THRESHOLD = 0.2


def _rank_channels_by_relevance(channels: list[dict], config: DigestConfig) -> list[dict]:
    from slack_digest.scorer import _get_model

    if not config.themes:
        return channels[:MAX_CHANNELS]

    model = _get_model()
    theme_texts = [f"{t.name}: {', '.join(t.keywords)}" for t in config.themes]
    theme_embeddings = model.encode(theme_texts, normalize_embeddings=True)

    channel_texts = [f"{ch['name']} {ch.get('topic', '')} {ch.get('purpose', '')}".strip() for ch in channels]
    channel_embeddings = model.encode(channel_texts, normalize_embeddings=True)

    scored = []
    for i, ch in enumerate(channels):
        similarities = channel_embeddings[i] @ theme_embeddings.T
        best_score = float(similarities.max())
        if best_score >= CHANNEL_RELEVANCE_THRESHOLD:
            scored.append((ch, best_score))

    scored.sort(key=lambda x: x[1], reverse=True)
    ranked = [ch for ch, _ in scored[:MAX_CHANNELS]]
    top_names = ", ".join(f"#{ch['name']}" for ch in ranked[:10])
    logger.info(f"Top channels by theme relevance: {top_names}...")
    return ranked


def _fetch_all_messages(client: WebClient, config: DigestConfig, fixed_lookback_hours: int | None = None) -> tuple[list[dict], dict]:
    logger.info("Fetching channel list...")
    channels = get_all_public_channels(client)
    logger.info(f"Found {len(channels)} public channels")
    excluded = set(config.exclude_channels)
    included = set(config.include_channels) if config.include_channels else None

    filtered_channels = []
    for ch in channels:
        if ch["name"] in excluded:
            continue
        if included and ch["name"] not in included:
            continue
        filtered_channels.append(ch)

    filtered_channels = _rank_channels_by_relevance(filtered_channels, config)
    logger.info(f"Scanning {len(filtered_channels)} channels")

    if fixed_lookback_hours:
        oldest = time.time() - (fixed_lookback_hours * 3600)
        period_desc = f"Last {fixed_lookback_hours} hours"
    else:
        last_run = _load_last_run()
        if last_run:
            oldest = last_run.timestamp()
            period_desc = f"Since {last_run.strftime('%A %b %d, %H:%M')}"
        else:
            oldest = time.time() - (config.digest.lookback_hours * 3600)
            period_desc = f"Last {config.digest.lookback_hours} hours"
    all_messages: list[dict] = []

    for i, ch in enumerate(filtered_channels):
        logger.info(f"  [{i + 1}/{len(filtered_channels)}] #{ch['name']} ({ch.get('num_members', '?')} members)")
        messages = get_channel_messages(client, ch["id"], oldest, config.digest.max_messages_per_channel)

        user_ids = {m["user"] for m in messages if m["user"] != "unknown"}
        user_map = {}
        for uid in user_ids:
            info = get_user_info(client, uid)
            user_map[uid] = info.get("display_name") or info.get("real_name") or info.get("name", uid)

        for msg in messages:
            msg["channel_name"] = ch["name"]
            msg["channel_id"] = ch["id"]
            msg["author_name"] = user_map.get(msg["user"], msg["user"])

        all_messages.extend(messages)
        if messages:
            logger.info(f"    → {len(messages)} messages")


    stats = {
        "channels_scanned": len(filtered_channels),
        "messages_processed": len(all_messages),
        "period": period_desc,
    }
    logger.info(f"Fetched {len(all_messages)} messages from {len(filtered_channels)} channels")
    return all_messages, stats


def _fetch_threads_for_top_messages(client: WebClient, scored_messages):
    threads_to_fetch = [m for m in scored_messages if m.reply_count >= 3 and m.score >= 0.4]
    if threads_to_fetch:
        logger.info(f"Fetching {len(threads_to_fetch)} threads...")
    for msg in threads_to_fetch:
        replies = get_thread_replies(client, msg.channel_id, msg.ts)
        for reply in replies:
            info = get_user_info(client, reply.get("user", "unknown"))
            reply["author_name"] = info.get("display_name") or info.get("real_name") or info.get("name", reply.get("user", "?"))
        msg.thread_replies = replies


def _run_digest(
    config: DigestConfig | None = None,
    reader_client: WebClient | None = None,
    bot_client: WebClient | None = None,
    fixed_lookback_hours: int | None = None,
    workspace_url: str = "https://lovable-dev.slack.com",
) -> None:
    from slack_digest.config import get_config

    cfg = config or get_config()

    all_messages, stats = _fetch_all_messages(reader_client, cfg, fixed_lookback_hours)

    scorer = MessageScorer(cfg)
    scored = scorer.score(all_messages)
    logger.info(f"Scored {len(scored)} relevant messages from {stats['messages_processed']} total")

    if not scored:
        logger.info("No relevant messages found — sending short notice")
        send_dm(
            bot_client,
            cfg.target_user.slack_id,
            [{"type": "section", "text": {"type": "mrkdwn", "text": f"Nothing notable in the {stats['period'].lower()}. :sunglasses:"}}],
            text="Nothing notable today.",
        )
        return

    _fetch_threads_for_top_messages(reader_client, scored)

    stats["messages_highlighted"] = len(scored)
    logger.info("Sending to Claude for synthesis...")
    digest = generate_digest(scored, cfg, stats, workspace_url)

    blocks = format_digest_blocks(digest)
    send_dm(bot_client, cfg.target_user.slack_id, blocks, text=digest.get("one_liner", "Your daily Slack digest"))
    logger.info("Digest sent successfully")


def main() -> None:
    _validate_env()

    config = load_config()
    logger.info(
        f"Config loaded — schedule: {', '.join(config.digest.schedule)}, "
        f"themes: {len(config.themes)}, people: {len(config.people)}"
    )

    bot_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    reader_client = WebClient(token=os.environ["SLACK_USER_TOKEN"])

    try:
        auth = bot_client.auth_test()
        workspace_url = auth.get("url", "https://lovable-dev.slack.com")
        logger.info(f"Connected to Slack as {auth['bot_id']} in workspace {auth['team']}")
    except Exception as e:
        logger.error(f"Slack auth failed: {e}")
        sys.exit(1)

    config = _resolve_target_user(config, bot_client)

    # Warm up the embedding model at startup
    MessageScorer(config)

    def digest_callback(override_config: DigestConfig | None = None, fixed_lookback_hours: int | None = None):
        _run_digest(override_config, reader_client, bot_client, fixed_lookback_hours, workspace_url)

    scheduler = DigestScheduler(digest_callback, config)
    scheduler.start()
    logger.info(f"Scheduler started — digests at {', '.join(config.digest.schedule)}")

    bolt_app = create_app(config, bot_client, scheduler, digest_callback)
    handler = SocketModeHandler(bolt_app, os.environ["SLACK_APP_TOKEN"])

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        scheduler.stop()
        handler.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Slack Digest Agent started — listening for commands...")
    handler.start()


if __name__ == "__main__":
    main()
