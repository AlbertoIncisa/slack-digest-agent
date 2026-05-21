from __future__ import annotations

import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_user_cache: dict[str, dict] = {}


def _retry_on_rate_limit(func):
    def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except SlackApiError as e:
                if e.response.status_code == 429:
                    retry_after = int(e.response.headers.get("Retry-After", 10))
                    logger.warning(f"Rate limited, retrying in {retry_after}s (attempt {attempt + 1}/3)")
                    time.sleep(retry_after)
                    continue
                raise
        raise SlackApiError("Rate limit retries exhausted", response=None)

    return wrapper


@_retry_on_rate_limit
def get_all_public_channels(client: WebClient) -> list[dict]:
    channels = []
    cursor = None
    while True:
        response = client.conversations_list(
            types="public_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        for ch in response["channels"]:
            channels.append(
                {
                    "id": ch["id"],
                    "name": ch["name"],
                    "num_members": ch.get("num_members", 0),
                    "topic": ch.get("topic", {}).get("value", ""),
                    "purpose": ch.get("purpose", {}).get("value", ""),
                }
            )
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    channels.sort(key=lambda c: c["num_members"], reverse=True)
    return channels


@_retry_on_rate_limit
def get_channel_messages(
    client: WebClient,
    channel_id: str,
    oldest: float,
    limit: int = 200,
) -> list[dict]:
    messages = []
    cursor = None
    while len(messages) < limit:
        try:
            response = client.conversations_history(
                channel=channel_id,
                oldest=str(oldest),
                limit=min(200, limit - len(messages)),
                cursor=cursor,
            )
        except SlackApiError as e:
            if e.response.get("error") in ("channel_not_found", "not_in_channel"):
                logger.debug(f"Cannot access channel {channel_id}: {e.response.get('error')}")
                return []
            raise

        for msg in response["messages"]:
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                continue
            messages.append(
                {
                    "user": msg.get("user", "unknown"),
                    "text": msg.get("text", "")[:500],
                    "ts": msg["ts"],
                    "reply_count": msg.get("reply_count", 0),
                    "reactions": [{"name": r["name"], "count": r["count"]} for r in msg.get("reactions", [])],
                    "thread_ts": msg.get("thread_ts"),
                }
            )

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return messages


@_retry_on_rate_limit
def get_thread_replies(client: WebClient, channel_id: str, thread_ts: str) -> list[dict]:
    response = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=50)
    replies = []
    for msg in response.get("messages", [])[1:]:  # skip the parent message
        replies.append(
            {
                "user": msg.get("user", "unknown"),
                "text": msg.get("text", "")[:500],
                "ts": msg["ts"],
            }
        )
    return replies


def get_user_info(client: WebClient, user_id: str) -> dict:
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        response = client.users_info(user=user_id)
        profile = response["user"].get("profile", {})
        info = {
            "id": user_id,
            "name": response["user"].get("name", ""),
            "real_name": profile.get("real_name", ""),
            "display_name": profile.get("display_name", ""),
            "title": profile.get("title", ""),
        }
        _user_cache[user_id] = info
        return info
    except SlackApiError:
        return {"id": user_id, "name": user_id, "real_name": "", "display_name": "", "title": ""}


def lookup_user_by_email(client: WebClient, email: str) -> str | None:
    try:
        response = client.users_lookupByEmail(email=email)
        return response["user"]["id"]
    except SlackApiError as e:
        logger.error(f"Failed to look up user by email {email}: {e}")
        return None


def send_dm(client: WebClient, user_id: str, blocks: list[dict], text: str) -> None:
    client.chat_postMessage(channel=user_id, blocks=blocks, text=text)


def clear_user_cache() -> None:
    _user_cache.clear()
