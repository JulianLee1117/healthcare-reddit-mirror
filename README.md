# healthcare-reddit-mirror

A live mirror of r/healthcare that polls Reddit every 5 minutes, sends new-post events to Amplitude, and displays the current front page in a web UI.

**Live:** https://julianlee1117--healthcare-reddit-mirror-serve.modal.run

## Architecture

Single-file ASGI app (`app.py`) deployed on Modal with two functions:

1. **Poller** (`poll_reddit`) — Cron-scheduled every 5 min. Fetches r/healthcare via RSS, deduplicates by post ID, sends `reddit_post_ingested` events to Amplitude for new posts, and caches the current front page in Modal Dict.

2. **Web server** (`serve`) — FastAPI app rendering server-side HTML from the Dict cache. Sub-10ms response times (no Reddit call per page load). Tracks page views to Amplitude via background tasks.

```
Reddit RSS ──> poll_reddit ──> Amplitude (reddit_post_ingested)
                   │
                   ▼
              Modal Dict ──> serve (GET /) ──> HTML
                                │
                                ▼
                          Amplitude (mirror_page_viewed)
```

### Why RSS over JSON

Reddit blocks `.json` requests from cloud/datacenter IPs (returns 403 from Modal). The RSS feed (`/r/healthcare.rss`) returns 200 with no auth required and contains all needed fields: post ID, title, link, author, and timestamp. This eliminates OAuth credential management entirely.

### Dedup strategy

Two layers: a `seen_ids` set in Modal Dict (primary) and Amplitude's `insert_id` field (crash-recovery safety net). If the poller crashes after sending events but before updating `seen_ids`, the next run re-sends — but Amplitude deduplicates via `insert_id` within the same `device_id`.

## Setup

### Prerequisites

- [Modal](https://modal.com) account with `modal` CLI installed
- [Amplitude](https://amplitude.com) project with an API key

### Configure secrets

```bash
modal secret create amplitude-secret AMPLITUDE_API_KEY=<your-key>
```

### Deploy

```bash
modal deploy app.py
```

This creates a persistent URL and activates the 5-minute cron. Posts appear in the web UI after the first poll cycle.

### Local development

```bash
modal serve app.py        # hot-reloading dev server
modal run app.py::poll_reddit  # one-shot poll (useful for testing)
```

## Amplitude events

| Event | Triggered by | Key properties |
|---|---|---|
| `reddit_post_ingested` | Poller (new posts only) | `title`, `link`, `author`, `post_age_minutes`, `post_position`, `is_question`, `content_length` |
| `mirror_page_viewed` | Web UI page load | `post_count` |

## Agent transcript

This project was built with Claude Code. The full interaction transcript is in [`transcript.md`](transcript.md).
