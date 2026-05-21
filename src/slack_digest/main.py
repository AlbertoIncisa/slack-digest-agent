from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    get_recent_message_texts,
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


MAX_CHANNELS = 200
CHANNEL_RELEVANCE_THRESHOLD = 0.2
CHANNEL_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / ".channel_cache.json"
CHANNEL_CACHE_MAX_AGE_DAYS = 7



CHANNEL_EMBEDDINGS_FILE = Path(__file__).resolve().parent.parent.parent / ".channel_embeddings.npy"
CHANNEL_SAMPLE_MESSAGES = 20


def _build_channel_description(ch: dict, sample_text: str) -> str:
    parts = [ch["name"], ch.get("topic", ""), ch.get("purpose", "")]
    if sample_text:
        parts.append(sample_text[:500])
    return " ".join(p for p in parts if p).strip()


def _load_cache() -> tuple[list[dict], any, datetime | None]:
    import numpy as np
    try:
        cache = json.loads(CHANNEL_CACHE_FILE.read_text())
        embeddings = np.load(CHANNEL_EMBEDDINGS_FILE)
        channels = cache["channels"]
        scanned_at = datetime.fromisoformat(cache["scanned_at"])
        if len(embeddings) != len(channels):
            return [], None, None
        return channels, embeddings, scanned_at
    except (OSError, ValueError, KeyError):
        return [], None, None


def _save_cache(channels: list[dict], embeddings) -> None:
    import numpy as np
    try:
        cache = {"scanned_at": datetime.now().isoformat(), "channels": channels}
        CHANNEL_CACHE_FILE.write_text(json.dumps(cache, indent=2))
        np.save(CHANNEL_EMBEDDINGS_FILE, embeddings)
    except OSError:
        logger.warning("Could not write channel cache")


def scan_and_cache_channels(client: WebClient, config: DigestConfig) -> list[dict]:
    import numpy as np
    from slack_digest.scorer import _get_model

    logger.info("Fetching channel list from Slack...")
    live_channels = get_all_public_channels(client)
    live_ids = {ch["id"] for ch in live_channels}
    logger.info(f"Found {len(live_channels)} public channels")

    cached_channels, cached_embeddings, _ = _load_cache()
    cached_by_id = {ch["id"]: (i, ch) for i, ch in enumerate(cached_channels)}

    new_channels = [ch for ch in live_channels if ch["id"] not in cached_by_id]
    removed_ids = set(cached_by_id.keys()) - live_ids
    kept_indices = [i for ch_id, (i, _) in cached_by_id.items() if ch_id not in removed_ids]

    logger.info(f"Channels: {len(kept_indices)} existing, {len(new_channels)} new, {len(removed_ids)} removed")

    model = _get_model()
    embed_dim = model.get_sentence_embedding_dimension()

    if new_channels:
        logger.info(f"Fetching sample messages for {len(new_channels)} new channels...")
        new_texts = []
        for j, ch in enumerate(new_channels):
            if (j + 1) % 50 == 0:
                logger.info(f"  [{j + 1}/{len(new_channels)}]")
            sample = get_recent_message_texts(client, ch["id"], CHANNEL_SAMPLE_MESSAGES)
            new_texts.append(_build_channel_description(ch, sample))
        new_embeddings = model.encode(new_texts, normalize_embeddings=True)
    else:
        new_embeddings = np.empty((0, embed_dim))

    if cached_embeddings is not None and len(kept_indices) > 0:
        kept_channels = [cached_channels[i] for i in kept_indices]
        kept_embeddings = cached_embeddings[kept_indices]
    else:
        kept_channels = []
        kept_embeddings = np.empty((0, embed_dim))

    all_channels = kept_channels + new_channels
    all_embeddings = np.vstack([kept_embeddings, new_embeddings]) if len(all_channels) > 0 else np.empty((0, embed_dim))

    _save_cache(all_channels, all_embeddings)
    logger.info(f"Cached {len(all_channels)} channels + embeddings")

    return _select_top_channels(all_channels, all_embeddings, config)


def _select_top_channels(channels: list[dict], channel_embeddings, config: DigestConfig) -> list[dict]:
    from slack_digest.scorer import _get_model

    excluded = set(config.exclude_channels)

    if not config.themes:
        return [ch for ch in channels if ch["name"] not in excluded][:MAX_CHANNELS]

    model = _get_model()
    theme_texts = [f"{t.name}: {', '.join(t.keywords)}" for t in config.themes]
    theme_embeddings = model.encode(theme_texts, normalize_embeddings=True)

    scored = []
    for i, ch in enumerate(channels):
        if ch["name"] in excluded:
            continue
        similarities = channel_embeddings[i] @ theme_embeddings.T
        best_score = float(similarities.max())
        if best_score >= CHANNEL_RELEVANCE_THRESHOLD:
            scored.append((ch, best_score))

    scored.sort(key=lambda x: x[1], reverse=True)
    ranked = [ch for ch, _ in scored[:MAX_CHANNELS]]
    top_names = ", ".join(f"#{ch['name']}" for ch in ranked[:10])
    logger.info(f"Top channels by theme relevance: {top_names}...")
    return ranked


def _get_channels(client: WebClient, config: DigestConfig) -> list[dict]:
    cached_channels, cached_embeddings, scanned_at = _load_cache()
    if cached_embeddings is not None and scanned_at is not None:
        age = datetime.now() - scanned_at
        if age.days < CHANNEL_CACHE_MAX_AGE_DAYS:
            logger.info(f"Using cached channels ({len(cached_channels)} total, scanned {age.days}d ago)")
            return _select_top_channels(cached_channels, cached_embeddings, config)
        else:
            logger.info(f"Channel cache is {age.days} days old — refreshing")
    return scan_and_cache_channels(client, config)


def _fetch_all_messages(client: WebClient, config: DigestConfig, fixed_lookback_hours: int | None = None) -> tuple[list[dict], dict]:
    filtered_channels = _get_channels(client, config)
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

    bolt_app = create_app(config, bot_client, scheduler, digest_callback, reader_client)
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
