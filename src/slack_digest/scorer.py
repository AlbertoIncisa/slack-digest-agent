from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from sentence_transformers import SentenceTransformer

from slack_digest.config import DigestConfig

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.25
TOP_N = 80
REACTION_WEIGHT = 0.05
REPLY_WEIGHT = 0.03
TRACKED_AUTHOR_BONUS = 0.15
LENGTH_BONUS_THRESHOLD = 100

PRIORITY_MULTIPLIER = {
    "critical": 1.5,
    "high": 1.25,
    "medium": 1.0,
    "low": 0.75,
}

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("Loading embedding model...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Embedding model loaded")
    return _model


@dataclass
class ScoredMessage:
    channel_name: str
    channel_id: str
    author_name: str
    author_id: str
    text: str
    ts: str
    reply_count: int
    reactions: list[dict]
    score: float
    matched_themes: list[tuple[str, float]] = field(default_factory=list)
    thread_replies: list[dict] | None = None


class MessageScorer:
    def __init__(self, config: DigestConfig):
        self.model = _get_model()
        self.tracked_user_ids = {p.slack_id for p in config.people}

        theme_texts = [f"{t.name}: {', '.join(t.keywords)}" for t in config.themes]
        if theme_texts:
            self.theme_embeddings = self.model.encode(theme_texts, normalize_embeddings=True)
        else:
            self.theme_embeddings = np.array([])
        self.theme_names = [t.name for t in config.themes]
        self.theme_priorities = {t.name: t.priority for t in config.themes}

        logger.info(f"Scorer ready — {len(self.theme_names)} themes embedded")

    def score(self, messages: list[dict]) -> list[ScoredMessage]:
        if not messages:
            return []

        texts = [m.get("text", "") or "" for m in messages]
        msg_embeddings = self.model.encode(texts, normalize_embeddings=True)

        scored: list[ScoredMessage] = []
        for i, msg in enumerate(messages):
            theme_scores: list[tuple[str, float]] = []
            max_similarity = 0.0

            if len(self.theme_embeddings) > 0:
                similarities = msg_embeddings[i] @ self.theme_embeddings.T
                for j, sim in enumerate(similarities):
                    raw_sim = float(sim)
                    if raw_sim > SIMILARITY_THRESHOLD:
                        theme_scores.append((self.theme_names[j], raw_sim))
                    priority = self.theme_priorities.get(self.theme_names[j], "medium")
                    weighted = raw_sim * PRIORITY_MULTIPLIER.get(priority, 1.0)
                    max_similarity = max(max_similarity, weighted)

            reaction_score = min(
                sum(r.get("count", 0) for r in msg.get("reactions", [])) * REACTION_WEIGHT,
                0.3,
            )
            reply_score = min(msg.get("reply_count", 0) * REPLY_WEIGHT, 0.2)
            author_bonus = TRACKED_AUTHOR_BONUS if msg.get("user") in self.tracked_user_ids else 0.0
            length_bonus = 0.05 if len(msg.get("text", "")) > LENGTH_BONUS_THRESHOLD else 0.0

            total = max_similarity + reaction_score + reply_score + author_bonus + length_bonus

            if total > SIMILARITY_THRESHOLD or author_bonus > 0:
                scored.append(ScoredMessage(
                    channel_name=msg.get("channel_name", ""),
                    channel_id=msg.get("channel_id", ""),
                    author_name=msg.get("author_name", msg.get("user", "unknown")),
                    author_id=msg.get("user", "unknown"),
                    text=msg.get("text", ""),
                    ts=msg["ts"],
                    reply_count=msg.get("reply_count", 0),
                    reactions=msg.get("reactions", []),
                    score=total,
                    matched_themes=sorted(theme_scores, key=lambda x: x[1], reverse=True),
                ))

        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:TOP_N]
