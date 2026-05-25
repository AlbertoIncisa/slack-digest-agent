# Feedback Loop Spec

## Problem

Themes in `config.yaml` go stale — what matters drifts over time, and users
don't manually update keywords or priorities. Digest items that aren't relevant
keep appearing; relevant things get missed.

## Solution: Two Changes

### Change 1: Feedback Capture

Add thumbs-up / thumbs-down buttons to each digest item. Store votes in a
local SQLite database.

#### Digest UI changes

Each digest item currently renders as a `section` block with an optional "View"
button accessory (`agent.py:format_digest_blocks`). Change this to:

1. A `section` block (summary text only — no accessory)
2. An `actions` block below it with three buttons:

```
[section: summary text]
[actions: 👍  👎  View →]
```

- Thumbs up: `action_id = "feedback_up"`, `value = "<feedback_id>"`
- Thumbs down: `action_id = "feedback_down"`, `value = "<feedback_id>"`
- View: same external link button as today

Each item's `actions` block gets a stable `block_id`:
`feedback_actions_<feedback_id>` (Slack allows alphanumeric + `_` `-`).
This is required for `chat.update` after a vote — replace only that block,
not the whole message.

After a user clicks thumbs up or down, acknowledge the interaction and replace
that item's `actions` block with a `context` block:
"You found this useful" or "Noted — will tune".

#### Slack 50-block limit

Slack allows at most **50 blocks per message**. Today each item is 1 block;
adding an `actions` block per item makes it 2. A digest with ~20 items can
exceed the limit. The existing truncation in `format_digest_blocks` silently
drops tail items — unacceptable once feedback buttons are item-scoped.

**Decision: split into multiple DMs when over the limit.**

- Reserve 4 blocks for overhead per message (header or continuation header,
  TL;DR on first message only, dividers, footer).
- Budget ~22 item pairs (section + actions) per message → ~48 blocks.
- When items exceed the budget, send message 1 of N, then message 2 of N, etc.
- Continuation messages get a short header: `Daily Digest (2/3) — Monday, May 25`
- Footer stats appear only on the last message.
- Never truncate items silently.

Remove the current `blocks[:MAX_BLOCKS - 1]` truncation once splitting is
implemented. Keep `MAX_BLOCKS = 50` as the per-message ceiling.

#### feedback_id and source metadata

The `feedback_id` is a stable key generated at digest-send time. Format:

```
<digest_run_id>_<section_idx>_<item_idx>
```

- `digest_run_id`: UTC timestamp without punctuation, e.g. `20260525T093000Z`
  — avoids collisions when two digests run the same calendar day (`/digest-now`
  + scheduled).
- Example: `20260525T093000Z_0_2`

This format is safe for both the `feedback_id` value and the Slack `block_id`
(`feedback_actions_20260525T093000Z_0_2`) — Slack allows alphanumeric + `_` `-`
in block IDs.

**Scorer metadata is not in Claude's digest JSON.** Themes, score, and
`raw_text` live on `ScoredMessage` in `scorer.py`. Before rendering blocks,
`generate_digest` must attach a lookup map to the digest dict:

```python
digest["_source_by_message_id"] = {
    f"msg_{idx}": {
        "themes": [(name, score) for name, score in msg.matched_themes],
        "score": msg.score,
        "raw_text": msg.text,
        "channel_id": msg.channel_id,
        "ts": msg.ts,
    }
    for idx, msg in enumerate(scored_messages)
}
```

`format_digest_blocks` reads `item["message_id"]` → `_source_by_message_id`
to populate `digest_items`. Do not ask Claude to include themes or scores in
its JSON output.

Also store `digest_run_id` on every row in `digest_items` and `feedback`.

#### Storage

New file: `feedback.py`. SQLite database at `~/.slack-digest/feedback.db`
(configurable via `DIGEST_FEEDBACK_DB` env var). Create parent directory on
first open. Validate `feedback_id` against `^[\w]+$` before use.

```sql
CREATE TABLE feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id   TEXT NOT NULL UNIQUE,   -- upsert key
    digest_run_id TEXT NOT NULL,
    digest_date   TEXT NOT NULL,          -- ISO date (for display/grouping)
    section       TEXT NOT NULL,
    channel       TEXT NOT NULL,
    author        TEXT NOT NULL,
    summary       TEXT NOT NULL,
    relevance     TEXT,
    themes        TEXT,                   -- JSON array of {name, score}
    score         REAL,
    vote          TEXT NOT NULL,          -- "up" or "down"
    voted_at      TEXT NOT NULL,          -- ISO timestamp
    raw_text      TEXT
);

CREATE TABLE digest_items (
    feedback_id   TEXT PRIMARY KEY,
    digest_run_id TEXT NOT NULL,
    digest_date   TEXT NOT NULL,
    section       TEXT NOT NULL,
    channel       TEXT NOT NULL,
    author        TEXT NOT NULL,
    summary       TEXT NOT NULL,
    relevance     TEXT,
    themes        TEXT,
    score         REAL,
    raw_text      TEXT
);
```

Flow:

1. `_run_digest` generates `digest_run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")`
2. `generate_digest` attaches `_source_by_message_id`
3. `format_digest_blocks(digest, digest_run_id)` saves each item to
   `digest_items`, returns block lists (possibly multiple messages)
4. User clicks button → handler looks up `feedback_id` in `digest_items`,
   upserts into `feedback` on `feedback_id`
5. Duplicate votes on the same `feedback_id` overwrite — user can change their
   mind; only the latest vote counts toward tuning thresholds

#### Bot handler

Register specific handlers **before** the existing catch-all
`@app.action(re.compile(".*"))` in `bot.py` (or remove the catch-all once
real handlers exist):

```python
@app.action("feedback_up")
@app.action("feedback_down")
def handle_feedback_action(ack, body, client):
    ack()
    # record_feedback → chat.update replacing block_id feedback_actions_<id>
```

Both call `feedback.record_feedback(feedback_id, vote)`, then
`tuner.maybe_run()` to check if tuning should trigger.

After recording, update the message UI: `chat.update` replaces the **entire**
blocks array, not a single block. The handler must:

1. Read `body["message"]["blocks"]` (Slack includes the full current array)
2. Find the block where `block_id == f"feedback_actions_{feedback_id}"`
3. Replace it with a `context` block ("You found this useful" / "Noted — will tune")
4. Send the full modified array back via `client.chat_update(channel=..., ts=..., blocks=...)`

This works transparently with multi-message digests — each DM has its own `ts`,
and Slack's action payload includes the `ts` and `channel` of the specific
message the button lives in. No need to track which items went into which DM.

#### What does NOT change (Change 1)

- The scoring pipeline (`scorer.py`) is untouched
- The Claude synthesis prompt (`agent.py`) is untouched
- `config.yaml` schema is untouched
- No new Slack OAuth scopes needed (interactivity is already enabled)

---

### Change 2: Feedback-Driven Theme Tuning

Analyzes accumulated feedback and proposes changes to themes and scoring
parameters. Triggered by feedback density, not a fixed schedule.

#### Trigger

Evaluated on every new vote (including upserts — a changed vote updates
`voted_at` and the vote value). Four conditions must all be true:

1. **Enough signal**: >= `tuning.min_votes` unprocessed votes
2. **Cooldown**: >= `tuning.cooldown_days` since last tuning **attempt**
   (any `tuning_log` row — prevents hammering Claude after a dismiss)
3. **Pain threshold**: > `tuning.pain_threshold` of unprocessed votes are
   thumbs-down
4. **No pending proposal**: no `tuning_log` row with `status = 'pending'`

**Unprocessed votes** = rows in `feedback` where `voted_at` is after the
most recent **applied** tuning run's `applied_at`. If no tuning has ever been
applied, all votes count.

Votes are **not** consumed when a proposal is generated or dismissed — only
when the user clicks Apply (or when `auto_apply: true`). This means a bad
proposal can be dismissed and the same votes will re-trigger auto-tune once
cooldown expires.

There is no separate `processed` flag on `feedback` — the applied-run
timestamp is the watermark.

If all four pass, the tuner runs automatically. If themes are working well
(few votes or mostly thumbs-up), it stays quiet indefinitely.

**Intentional limitation:** auto-tune only fires on pain (high thumbs-down
ratio). It will not detect "digest is fine but incomplete" (all thumbs-up on
what appeared, but relevant channels missed). Use `/digest-tune` for that.

Users can also force a run via `/digest-tune` regardless of these conditions.

```yaml
# addition to config.yaml schema
tuning:
  auto_apply: false       # if true, apply changes without confirmation
  min_votes: 10           # minimum unprocessed votes before tuning can trigger
  cooldown_days: 3        # minimum days between tuning runs
  pain_threshold: 0.3     # minimum thumbs-down ratio to trigger (0.0-1.0)
```

#### Analysis flow

New file: `tuner.py`.

1. **Gather data**: Query unprocessed votes (same definition as trigger:
   `voted_at > last applied_at`, capped at last 4 weeks). Split into
   thumbs-up and thumbs-down lists.

2. **Build prompt**: Send to Claude with the current `config.yaml` themes and
   scoring defaults:

   ```
   Current themes:
   - Incidents (critical): incident, outage, downtime, P0, P1, SEV
   - Engineering (medium): deploy, PR, merge, architecture, refactor

   Global similarity threshold: 0.25

   Items the user liked (thumbs up):
   [section, channel, summary, matched themes, score] x N

   Items the user disliked (thumbs down):
   [section, channel, summary, matched themes, score] x N

   Analyze the patterns. Return a JSON object with suggested changes:
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
   ```

   **Note:** Priority changes go through `theme_changes` (update the
   `priority` field). Do not expose `priority_multiplier` — the scorer maps
   priority levels to fixed multipliers; there is no per-theme multiplier in
   config today.

3. **Output**: The tuner produces a structured diff of proposed changes.

#### Where the trigger check lives

`bot.py` calls `feedback.record_feedback(...)` then `tuner.maybe_run()` —
two separate calls, no import between `feedback.py` and `tuner.py`.
`feedback.py` is pure storage; `tuner.py` reads from it but is never imported
by it.

`tuner.maybe_run()` evaluates the four conditions. If all pass, it spawns the
tuning job in a background thread (same pattern as `/digest-now`).

Use a module-level lock so concurrent votes cannot spawn duplicate tuning
jobs. `maybe_run()` acquires the lock, re-checks conditions, then spawns the
thread and releases.

`/digest-tune` calls `tuner.force_run()` which skips signal/cooldown/pain
conditions but still refuses to run if a `pending` proposal exists (user must
Apply or Dismiss first).

#### Tuning proposal lifecycle

Each tuner run creates one `tuning_log` row and progresses through statuses:

```
pending → applied   (user clicks Apply, or auto_apply: true)
pending → dismissed (user clicks Dismiss)
```

1. **Run starts**: Claude returns proposed changes. Insert `tuning_log` row
   with `status = 'pending'`, `run_date = now`, vote counts, full JSON
   `changes`, and `config_before` snapshot (themes + scoring — captured
   before any apply, even if auto_apply).
2. **`auto_apply: false`**: Send proposal DM with Apply / Dismiss buttons.
   Votes remain unprocessed (`status` still `pending`).
3. **Apply** (`action_id = "tuning_apply"`, `value = "<tuning_log_id>"`):
   - Validate row is `pending`
   - Write theme/scoring changes to `config.yaml`
   - Set `status = 'applied'`, `applied_at = now`
   - Votes with `voted_at <= applied_at` are now processed (watermark moves)
   - Update proposal DM to show "Applied"
4. **Dismiss** (`action_id = "tuning_dismiss"`, `value = "<tuning_log_id>"`):
   - Validate row is `pending`
   - Set `status = 'dismissed'`, `dismissed_at = now`
   - Votes stay unprocessed — eligible to re-trigger after cooldown
   - Update proposal DM to show "Dismissed"
5. **`auto_apply: true`**: Apply immediately after Claude returns (same as
   step 3), then send summary DM (no buttons).

Register `tuning_apply` and `tuning_dismiss` handlers in `bot.py` before the
catch-all regex, same as feedback handlers.

#### Applying changes

Two modes controlled by `tuning.auto_apply`:

- **`false` (default)**: Send a DM with proposed changes and Apply / Dismiss
  buttons. Config is unchanged until Apply.

- **`true`**: Apply changes directly after Claude returns. Still send a
  summary DM so the user can revert via `/digest-tune revert`.

#### Reverting changes

`/digest-tune revert` restores `config_before` from the most recent
**applied** tuning run (`status = 'applied'`) and writes themes + scoring
back to `config.yaml`. Does not undo the vote watermark — votes processed
by that run stay processed.

`/digest-tune history` lists recent tuning runs: `run_date`, vote counts,
status (pending/applied/dismissed), and one-line `observations` summary.

Manual `/digest-themes add|remove` still works for ad-hoc edits but is not
a substitute for reverting an auto-tuned batch.

#### `/digest-tune` slash command

```
/digest-tune              # force analysis (skip signal/cooldown/pain checks)
/digest-tune history      # list recent tuning runs
/digest-tune revert       # restore config from last applied run
```

Blocked when a `pending` proposal exists — respond with "Apply or dismiss
the current proposal first."

#### What changes in config.yaml

Theme changes are straightforward — add/remove/update entries in the `themes`
list via existing `add_theme` / `remove_theme` helpers.

Scoring parameter changes require a new top-level `scoring` section (global
defaults) and an optional per-theme override:

```yaml
scoring:
  similarity_threshold: 0.25   # global default (replaces hardcoded constant)

themes:
  - name: Incidents
    keywords: [incident, outage, downtime, P0, P1, SEV]
    priority: critical
    similarity_threshold: 0.30   # optional per-theme override; null = use global
```

Add to `config.py`:

```python
class ScoringConfig(BaseModel):
    similarity_threshold: float = 0.25

class Theme(BaseModel):
    name: str
    keywords: list[str] = Field(default_factory=list)
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    similarity_threshold: float | None = None  # per-theme override

class TuningConfig(BaseModel):
    auto_apply: bool = False
    min_votes: int = 10
    cooldown_days: int = 3
    pain_threshold: float = 0.3

class DigestConfig(BaseModel):
    # ... existing fields ...
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    tuning: TuningConfig = Field(default_factory=TuningConfig)
```

The scorer reads `theme.similarity_threshold` if set, otherwise
`config.scoring.similarity_threshold`. Remove the hardcoded
`SIMILARITY_THRESHOLD = 0.25` constant from `scorer.py`.

When applying `scoring_changes`:
- `"theme": null` → update `config.scoring.similarity_threshold`
- `"theme": "Incidents"` → update that theme's `similarity_threshold`

`config_before` snapshot shape:

```json
{
  "themes": [ /* full themes list */ ],
  "scoring": { "similarity_threshold": 0.25 }
}
```

`MessageScorer` is recreated on each digest run, so config reload requires
no extra wiring.

#### Tuning history

```sql
CREATE TABLE tuning_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT NOT NULL,           -- when Claude analysis completed
    votes_up      INTEGER NOT NULL,
    votes_down    INTEGER NOT NULL,
    changes       TEXT NOT NULL,           -- full JSON response from Claude
    config_before TEXT NOT NULL,           -- JSON snapshot for revert
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
    applied_at    TEXT,                    -- watermark source; set on apply only
    dismissed_at  TEXT
);
```

Helper queries:

- **Unprocessed vote watermark**: `MAX(applied_at) FROM tuning_log WHERE status = 'applied'`
- **Cooldown check**: `MAX(run_date) FROM tuning_log` (any status)
- **Pending check**: `EXISTS (SELECT 1 FROM tuning_log WHERE status = 'pending')`

---

## Scope boundaries

Things this spec intentionally does NOT do:

- **No learned embedding vectors** — feedback tunes config, not the scoring
  model itself
- **No per-item re-scoring at query time** — the scorer stays stateless
  (reads config, scores messages, done)
- **No channel-level feedback** — only item-level. Channel relevance is
  derived from themes, so fixing themes fixes channels
- **No feedback on the digest structure** (section grouping, ordering) — only
  on individual items
- **No auto-tune on thumbs-up alone** — pain threshold required; use
  `/digest-tune` to analyze positive-only signal

## File changes summary

| File | Change |
|---|---|
| `feedback.py` | NEW — SQLite storage, vote upsert, digest item metadata |
| `tuner.py` | NEW — event-driven analysis on vote threshold, Claude prompt, config diff, revert |
| `agent.py` | Attach `_source_by_message_id` in `generate_digest`; update `format_digest_blocks` for feedback UI and multi-message split |
| `main.py` | Pass `digest_run_id` through digest pipeline; send multiple DMs if needed |
| `bot.py` | Add `feedback_up`/`feedback_down` handlers (before catch-all), apply/dismiss handlers, `/digest-tune` with `revert`/`history` subcommands |
| `config.py` | Add `ScoringConfig`, `TuningConfig`; add `similarity_threshold` to `Theme` |
| `scorer.py` | Read global + per-theme `similarity_threshold` from config |
| `scheduler.py` | No change — tuning is triggered by vote events, not scheduled |
| `config.example.yaml` | Add `tuning` section |
| `slack-app-manifest.yaml` | Add `/digest-tune` slash command |
