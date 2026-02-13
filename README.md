# healthcare-reddit-mirror

A live mirror of r/healthcare that polls Reddit every 5 minutes, sends new-post events to Amplitude, and displays the current front page in a web UI.

**Live:** https://lotus-health--reddit-mirror-serve.modal.run

## Architecture

`app.py` (backend) + React SPA (frontend), deployed on Modal:

1. **Poller** (`poll_reddit`) — Cron-scheduled every 5 min. Fetches r/healthcare via RSS from both `/hot` and `/new` feeds, deduplicates by post ID, sends `reddit_post_ingested` events to Amplitude for new posts, and caches the hot-sorted front page in Modal Dict.

2. **Web server** (`serve`) — FastAPI serving a React SPA + JSON API (`/api/posts`) from the Dict cache. No Reddit API call per page load (<10ms response).

```
Reddit RSS (/hot + /new) ──> poll_reddit ──> Amplitude (reddit_post_ingested)
                                  │
                                  ▼
                             Modal Dict ──> /api/posts ──> React SPA
```

### Why RSS instead of .json

Reddit blocks `.json` requests from cloud/datacenter IPs (returns 403 from Modal). The RSS feed (`/r/healthcare.rss`) returns 200 with no auth required and contains all fields the assessment requires: post ID, title, link, author, and timestamp. This eliminates OAuth credential management entirely.

### Dual-feed polling

The poller fetches both `/r/healthcare.rss` (hot) and `/r/healthcare/new.rss` to ensure comprehensive ingestion. Reddit's hot algorithm can bury zero-engagement posts — polling `/new` catches every post. The hot feed drives the web UI display (showing the actual front page), while both feeds contribute to Amplitude events.

### Dedup strategy

Two layers: a `seen_ids` set in Modal Dict (primary) and Amplitude's `insert_id` field (crash-recovery safety net). If the poller crashes after sending events but before updating `seen_ids`, the next run re-sends — but Amplitude deduplicates via `insert_id` within the same `device_id`.

### State storage

`modal.Dict` provides atomic per-key get/put with no configuration. r/healthcare generates ~5-10 posts/day — total data stays well under 1MB even after months. No external database needed.

## Setup

### Prerequisites

- Python 3.12+
- [Modal](https://modal.com) account with `modal` CLI installed
- [Amplitude](https://amplitude.com) project with an API key
- [Node.js](https://nodejs.org) (for building the frontend)

### Install & deploy

```bash
pip install -r requirements.txt
modal secret create amplitude-secret AMPLITUDE_API_KEY=<your-key>
cd frontend && npm install && npm run build && cd ..
modal deploy app.py
```

This creates a persistent URL and activates the 5-minute cron. Posts appear in the web UI after the first poll cycle.

### Local development

```bash
cd frontend && npm install && npm run build && cd ..
modal serve app.py               # hot-reloading dev server
modal run app.py::poll_reddit     # one-shot poll (useful for testing)
```

## Amplitude events

| Event | Triggered by | Key properties |
|---|---|---|
| `reddit_post_ingested` | Poller (new posts only) | `title`, `link`, `author`, `topic`, `is_question`, `has_content`, `post_age_minutes`, `post_position`, `content_length` |

Each `reddit_post_ingested` event uses the post author as `user_id` (format: `reddit:AuthorName`) to enable user-level analytics, and includes an `insert_id` of `reddit-{post_id}` for deduplication. Posts are auto-classified into one of 6 topics via keyword matching: `insurance_billing`, `policy_regulation`, `health_tech`, `career_workforce`, `patient_experience`, or `other`.

### Amplitude dashboard

Three charts built on the enriched event data, embedded in the web UI's **Analytics** tab:

| Chart | Type | What it shows | Growth insight |
|---|---|---|---|
| **Topic Breakdown** | Bar (grouped by `topic`) | Distribution of post topics across r/healthcare | Content opportunity identification — which categories dominate the community discourse |
| **Questions by Topic** | Bar (filtered `is_question=true`, grouped by `topic`) | Where people are actively asking for help | Pain point discovery — questions represent unmet needs a health product could address |
| **Post Volume by Day** | Line/bar (daily event count) | Community posting activity over time | Channel health monitoring — is the subreddit active enough to be a growth signal? |

## Documentation

This project was built with Claude Code.

- **[`PLAN.md`](PLAN.md)** — Implementation plan, iterated on and executed phase by phase. Includes architectural decisions, rationale, race condition analysis, and data store schema.
- **[`process.md`](process.md)** — Phase-by-phase execution log with timestamps, verification results, bugs encountered, and deviations from the plan.
- **[`transcript.md`](transcript.md)** — Raw Claude Code agent interaction transcript for most major implementations.
