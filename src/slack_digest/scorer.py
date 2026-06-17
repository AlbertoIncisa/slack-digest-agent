from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from sentence_transformers import SentenceTransformer

from slack_digest.config import DigestConfig

logger = logging.getLogger(__name__)

TOP_N = 80
REACTION_WEIGHT = 0.05
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
        self.global_threshold = config.scoring.similarity_threshold

        theme_texts = [f"{t.name}: {', '.join(t.keywords)}" for t in config.themes]
        if theme_texts:
            self.theme_embeddings = self.model.encode(theme_texts, normalize_embeddings=True)
        else:
            self.theme_embeddings = np.array([])
        self.theme_names = [t.name for t in config.themes]
        self.theme_priorities = {t.name: t.priority for t in config.themes}
        self.theme_thresholds = {
            t.name: t.similarity_threshold for t in config.themes if t.similarity_threshold is not None
        }
        self.channel_weights = {
            k.lstrip("#"): float(v) for k, v in config.scoring.channel_weights.items()
        }

        logger.info(f"Scorer ready — {len(self.theme_names)} themes embedded")

    def score(self, messages: list[dict]) -> list[ScoredMessage]:
        if not messages:
            return []

        texts = [m.get("text", "") or "" for m in messages]
        msg_embeddings = self.model.encode(texts, normalize_embeddings=True)

        scored: list[ScoredMessage] = []
        for i, msg in enumerate(messages):
            # A channel weight of <= 0 is a hard mute — nothing from it surfaces.
            channel_weight = self.channel_weights.get(msg.get("channel_name", "").lstrip("#"), 1.0)
            if channel_weight <= 0:
                continue

            # --- Relevance gate ---
            # A theme matches only if its raw cosine similarity clears that theme's
            # threshold (per-theme, falling back to the global threshold). Priority
            # and engagement play NO part in the gate — they only affect ranking.
            matched: list[tuple[str, float]] = []
            if len(self.theme_embeddings) > 0:
                similarities = msg_embeddings[i] @ self.theme_embeddings.T
                for j, sim in enumerate(similarities):
                    raw_sim = float(sim)
                    theme_name = self.theme_names[j]
                    threshold = self.theme_thresholds.get(theme_name, self.global_threshold)
                    if raw_sim > threshold:
                        matched.append((theme_name, raw_sim))

            is_tracked = msg.get("user") in self.tracked_user_ids
            if not matched and not is_tracked:
                continue

            # --- Ranking score (ordering only, never gating) ---
            # relevance of the best-matching theme, weighted by that theme's
            # priority, then scaled by engagement.
            engagement = 1.0
            engagement += min(sum(r.get("count", 0) for r in msg.get("reactions", [])) * REACTION_WEIGHT, 0.3)
            reply_count = msg.get("reply_count", 0)
            if reply_count > 0:
                engagement += 0.1 + min(reply_count * 0.01, 0.1)
            if len(msg.get("text", "")) > LENGTH_BONUS_THRESHOLD:
                engagement += 0.05

            best_weighted = max(
                (raw_sim * PRIORITY_MULTIPLIER.get(self.theme_priorities.get(name, "medium"), 1.0)
                 for name, raw_sim in matched),
                default=0.0,
            )
            # A soft channel weight (0 < w != 1) scales ranking only — it never
            # overrides the theme gate above.
            rank_score = best_weighted * engagement * channel_weight
            if is_tracked:
                rank_score = max(rank_score, TRACKED_AUTHOR_BONUS)

            scored.append(ScoredMessage(
                channel_name=msg.get("channel_name", ""),
                channel_id=msg.get("channel_id", ""),
                author_name=msg.get("author_name", msg.get("user", "unknown")),
                author_id=msg.get("user", "unknown"),
                text=msg.get("text", ""),
                ts=msg["ts"],
                reply_count=msg.get("reply_count", 0),
                reactions=msg.get("reactions", []),
                score=rank_score,
                matched_themes=sorted(matched, key=lambda x: x[1], reverse=True),
            ))

        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:TOP_N]
