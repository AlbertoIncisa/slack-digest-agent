# Slack Digest Agent

AI-powered daily Slack digest. Scans your workspace channels, scores messages against configurable themes using local embeddings, and sends a prioritized summary via DM.

## How it works

1. **Scan channels (weekly)** — fetches all public channels, reads last 20 messages from each to understand what the channel is about, embeds the descriptions, and caches everything locally. Subsequent scans are incremental — only new channels are fetched.
2. **Rank channels** — scores cached channel embeddings against your themes to pick the top 200 most relevant. Changes to themes take effect immediately without rescanning.
3. **Fetch messages** — reads messages from ranked channels since the last digest (not a fixed window — covers Friday to Monday after a weekend off).
4. **Score messages** — embeds messages locally, scores by semantic similarity to themes. Engagement (reactions, replies) acts as a multiplier, not an additive bonus — a popular off-topic message scores zero. Thread starters get a boost over standalone messages.
5. **Fetch threads** — expands the top 15 highest-scoring messages that have replies.
6. **Synthesize** — sends scored messages to Claude for synthesis into a structured digest with thematic sections.
7. **Send DM** — posts the digest with priority indicators and "View" buttons linking directly to source messages.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A Slack app with Socket Mode enabled
- An Anthropic API key

### Slack app configuration

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) with:

**Bot Token Scopes:** `chat:write`, `commands`, `users:read`, `users:read.email`

**User Token Scopes:** `channels:history`, `channels:read`, `users:read`

**Socket Mode:** Enabled (generates an app-level token starting with `xapp-`)

**Slash Commands:**
- `/digest-now` — Generate a digest immediately
- `/digest-config` — View or update settings
- `/digest-themes` — Manage themes
- `/digest-people` — Manage tracked people
- `/digest-rescan` — Rescan all channels and refresh the cache

### Install

```bash
git clone https://github.com/AlbertoIncisa/slack-digest-agent.git
cd slack-digest-agent
uv sync
```

### Configure

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your email, themes, schedule, etc.
```

Create `.envrc` (or export these variables):

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export SLACK_USER_TOKEN="xoxp-..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Run

```bash
source .envrc
.venv/bin/slack-digest
```

The embedding model (~80MB) downloads on first run. The first channel scan takes a few minutes (fetches sample messages from all channels). After that, startup is fast — cached embeddings load from disk.

## Configuration

Edit `config.yaml`:

```yaml
digest:
  schedule:
    - "09:30"
    - "18:00"
  timezone: Europe/Rome
  lookback_hours: 24  # fallback for first run only

themes:
  - name: Incidents
    keywords: [incident, outage, downtime, P0, P1]
    priority: critical  # 1.5x score multiplier
  - name: Sales
    keywords: [deal, prospect, demo, pricing]
    priority: high      # 1.25x
  - name: Engineering
    keywords: [deploy, PR, merge, architecture]
    priority: medium    # 1.0x

people:
  - slack_id: U01ABC123
    name: Jane Smith
    reason: VP Engineering

exclude_channels:
  - random
  - watercooler
```

### Themes

Themes use semantic matching, not exact keywords. A message about "VPC peering for a customer" scores high against both "Engineering" and "Enterprise" themes even without exact keyword overlap.

Priority levels affect ranking: `critical` (1.5x), `high` (1.25x), `medium` (1.0x), `low` (0.75x).

### Scoring

Message scores are multiplicative: `similarity × engagement`. This means:
- A popular off-topic message scores **zero** (no theme relevance = no score, regardless of reactions)
- A relevant message with high engagement ranks above an equally relevant quiet one
- Messages from tracked people always appear regardless of theme match

Engagement factors: reactions, reply count, thread-starter bonus, message length.

### Slash commands

- `/digest-now` — trigger immediately (covers since last digest)
- `/digest-now 8` — digest for the last 8 hours (fixed window)
- `/digest-config` — show current settings
- `/digest-config schedule 08:00` — change schedule
- `/digest-themes` — list themes
- `/digest-themes add "Security" alert,CVE,vulnerability high` — add a theme
- `/digest-people add @someone reason` — track a person
- `/digest-rescan` — rescan all channels and refresh embeddings cache

## Architecture

```
Weekly scan (cached)                 Daily digest
─────────────────                    ─────────────
                                     
Slack API: list all channels         Load cached embeddings
       │                                    │
       ▼                                    ▼
Fetch last 20 messages              Rank by themes (matrix multiply)
from each channel                          │
       │                                    ▼
       ▼                             Fetch messages (Slack API)
Embed name + topic +                       │
messages (local model)                      ▼
       │                             Score messages (local embed)
       ▼                                    │
Cache to disk ──────────────────────        ▼
(.channel_cache.json +               Top 80 → Claude API
 .channel_embeddings.npy)                   │
                                            ▼
                                     Send DM (bot token)
                                     with "View" buttons
```

All embeddings run locally on your machine. The only paid API call per digest is Claude for synthesis.

## Costs

- **Slack API:** Free
- **Embeddings:** Free (runs locally)
- **Claude API:** ~$0.01-0.05 per digest (single Sonnet call with ~80 messages)
