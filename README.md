# Slack Digest Agent

AI-powered daily Slack digest. Scans your channels, scores messages against configurable themes using local embeddings, and sends a prioritized summary via DM.

## How it works

1. **List channels** — fetches public channels you're a member of (uses your Slack user token)
2. **Pre-filter channels** — embeds channel names/topics against your themes, picks the most relevant ones
3. **Fetch messages** — reads recent messages since the last digest
4. **Score messages** — local embedding model (all-MiniLM-L6-v2) scores each message by semantic similarity to your themes, weighted by priority, reactions, replies, and tracked authors
5. **Fetch threads** — expands important threads with many replies
6. **Synthesize** — sends the top scored messages to Claude for synthesis into a structured digest
7. **Send DM** — posts the digest to you in Slack with priority indicators and "View" buttons linking to source messages

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

The embedding model (~80MB) downloads on first run. After that, startup takes a few seconds.

## Configuration

Edit `config.yaml`:

```yaml
digest:
  schedule:
    - "09:30"
    - "18:00"
  timezone: Europe/Rome
  lookback_hours: 24  # fallback for first run

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

### Slash commands

- `/digest-now` — trigger immediately (uses last-run timestamp as lookback)
- `/digest-now 8` — digest for the last 8 hours (fixed window)
- `/digest-config` — show current settings
- `/digest-config schedule 08:00` — change schedule
- `/digest-themes` — list themes
- `/digest-themes add "Security" alert,CVE,vulnerability high` — add a theme
- `/digest-people add @someone reason` — track a person

## Architecture

```
Slack API (user token)          Slack API (bot token)
       │                               ▲
       ▼                               │
  List channels                    Send DM
       │                               ▲
       ▼                               │
  Rank by theme ◄── Embeddings    Format blocks
  (local model)     (all-MiniLM)       ▲
       │                               │
       ▼                               │
  Fetch messages                  Claude API
       │                          (synthesis)
       ▼                               ▲
  Score messages ◄── Embeddings        │
  (local model)     (all-MiniLM)       │
       │                               │
       ▼                               │
  Top 80 messages ──────────────────────┘
```

All embedding runs locally on your machine (no API calls). The only external API call per digest is Claude for synthesis.

## Costs

- **Slack API:** Free
- **Embeddings:** Free (runs locally)
- **Claude API:** ~$0.01-0.05 per digest (single Sonnet call with ~80 messages)
