# Healthcare Reddit Mirror — Implementation Plan

## Context

Build and deploy a web app on Modal that continuously polls r/healthcare, sends new-post events to Amplitude, and displays current front-page posts in a web UI. The code must be correct, race-free, maintainable, concise, and secure.

**Architecture:** Single `app.py` file deployed on Modal with two functions:
1. A **scheduled poller** (every 5 min) that fetches Reddit, detects new posts, sends events to Amplitude, and caches posts in Modal Dict
2. An **ASGI web app** (FastAPI) that reads cached posts from Modal Dict and renders server-side HTML

**Why this approach:**
- Caching posts in Modal Dict keeps the web UI fast (<10ms) and avoids burning Reddit's unauthenticated rate limit (~10 req/min) on page loads
- `modal.Dict` provides atomic key-level get/put — no file-locking issues like Volume would have
- `modal.Cron` likely skips overlapping invocations (standard serverless pattern, not explicitly documented). Even if it doesn't, Amplitude `insert_id` dedup prevents duplicate events — the only consequence would be a redundant Amplitude POST
- Amplitude's `insert_id` dedup (7-day window) is the safety net if the poller crashes after sending events but before updating `seen_ids`

---

## Phase 1: Project Scaffolding & Modal Setup

**Goal:** Get a minimal Modal app deployed with a "hello world" web endpoint.

**File:** `app.py`

Create the Modal app skeleton:
- `modal.App("healthcare-reddit-mirror")`
- `modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")` (httpx is included via `fastapi[standard]`)
- A minimal FastAPI app with `GET /` returning "Hello"
- Wire it up with `@modal.asgi_app()`

**Verify:** Run `modal serve app.py`, visit the printed URL, confirm "Hello" response.

---

## Phase 2: Reddit Polling

**Goal:** Fetch r/healthcare.json, parse posts, and store them in Modal Dict.

**Add to `app.py`:**

1. Define `modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)`
2. Define constants: `REDDIT_URL`, `USER_AGENT` (must be descriptive, e.g. `modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)`)
3. Implement `_fetch_reddit() -> list[dict] | None`:
   - `httpx.get(REDDIT_URL, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)`
   - Parse `data["data"]["children"]` — extract per post: `id`, `title`, `author`, `permalink`, full `link` (`https://www.reddit.com` + permalink), `score`, `num_comments`, `created_utc`
   - Return `None` on any HTTP error (log it, don't crash)
4. Implement `poll_reddit()` function with `@app.function(schedule=modal.Cron("*/5 * * * *"))`:
   - Call `_fetch_reddit()`
   - Load `seen_ids` set from Dict (default to empty set if KeyError)
   - Compute `new_ids = current_ids - seen_ids`
   - Store `front_page` posts list in Dict
   - Update `seen_ids` in Dict (cap at 5000 to prevent unbounded growth)
   - (Amplitude sending is Phase 3 — for now just log new post count)

**Verify:** Run `modal run app.py::poll_reddit` — check logs show fetched posts and new IDs.

---

## Phase 3: Amplitude Integration

**Goal:** Send `reddit_post_ingested` events to Amplitude for each new post.

**Pre-req:** Create Amplitude org/project, get API key, then:
```bash
modal secret create amplitude-secret AMPLITUDE_API_KEY=<key>
```

**Add to `app.py`:**

1. Add `secrets=[modal.Secret.from_name("amplitude-secret")]` to the poller's `@app.function` decorator
2. Define constant `AMPLITUDE_URL = "https://api2.amplitude.com/2/httpapi"`
3. Implement `_send_to_amplitude(posts: list[dict]) -> None`:
   - Read `os.environ["AMPLITUDE_API_KEY"]`
   - Build events list — each event:
     ```python
     {
         "user_id": "reddit-mirror",
         "event_type": "reddit_post_ingested",
         "time": int(post["created_utc"] * 1000),  # ms
         "insert_id": f"reddit-{post['id']}",       # dedup safety net
         "event_properties": {
             "title": post["title"],
             "link": post["link"],
             "author": post["author"],
         },
     }
     ```
   - Send all events in one POST (r/healthcare returns ~25 posts max, well under Amplitude's 2000/request limit)
   - POST to `AMPLITUDE_URL` with `{"api_key": key, "events": events}`
   - Log errors but don't crash — poller should be resilient
4. Call `_send_to_amplitude(new_posts)` in `poll_reddit()` when `new_ids` is non-empty
5. Write `front_page` to Dict **before** `seen_ids` — optimizes for UI freshness; Amplitude `insert_id` handles any re-sends

**Verify:**
- `modal run app.py::poll_reddit` — check Amplitude dashboard for `reddit_post_ingested` events with correct title/link/author
- Run again — confirm no duplicate events in Amplitude (same `insert_id` values are deduped)

---

## Phase 4: Web UI

**Goal:** Render the cached front-page posts as clean server-side HTML.

**Add to `app.py`:**

1. Use `html.escape()` from stdlib for XSS prevention on user-generated Reddit content
2. Implement `_render_html(posts: list[dict]) -> str`:
   - HTML table with columns: score, title (as link), author, comment count
   - All dynamic values run through `html.escape()`
   - Clean minimal CSS (system font stack, max-width 800px, responsive)
   - Empty state message if no posts yet ("The poller runs every 5 minutes — check back shortly")
   - Auto-refresh via `<meta http-equiv="refresh" content="300">`
3. Update `GET /` endpoint:
   - Read `front_page` from `posts_dict` (catch KeyError, default to empty list)
   - Return `HTMLResponse(content=_render_html(posts))`
4. Add `GET /healthz` returning `{"status": "ok"}`
5. Add `@modal.concurrent(max_inputs=100)` on the serve function for efficient multi-request handling

**Verify:**
- `modal serve app.py` — visit URL, confirm posts render with title, link, author
- Check links open correct Reddit discussion pages in new tabs
- View page source — confirm no unescaped HTML in post titles/authors

---

## Phase 5: Deploy & End-to-End Verification

**Goal:** Deploy to production and verify the full pipeline.

1. `modal deploy app.py` — creates persistent URL + activates cron
2. Wait 5 minutes for first poll cycle
3. Visit web UI URL — posts should appear
4. Check Amplitude dashboard:
   - `reddit_post_ingested` events present with correct properties
   - Event count matches number of unique posts
   - No duplicates (verify via `insert_id`)
5. Wait for second poll cycle — confirm only genuinely new posts produce new events
6. Update `README.md` with:
   - What the app does
   - Setup instructions (Modal account, Amplitude secret)
   - How to deploy (`modal deploy app.py`)
   - Architecture overview

---

## Key Design Decisions

| Decision | Choice | Why |
|---|---|---|
| State storage | `modal.Dict` | Atomic get/put, no file locking, simple for small structured data |
| Polling frequency | Every 5 min (`modal.Cron`) | r/healthcare is low-traffic; avoids rate limits; Cron doesn't drift on redeploy |
| Web UI data source | Read from Dict cache | Sub-10ms response, no Reddit API call per page load |
| HTTP client | `httpx` (bundled with `fastapi[standard]`) | No extra dependency; async-capable; actively maintained |
| Template engine | f-string with `html.escape()` | Stdlib for XSS protection; no Jinja2 dependency for ~30 lines of HTML |
| Dedup strategy | `seen_ids` set in Dict + Amplitude `insert_id` | Belt and suspenders — Dict is primary, `insert_id` is crash-recovery safety net |
| `user_id` for Amplitude | `"reddit-mirror"` | System-generated events; satisfies 5-char minimum |
| Write order in poller | `front_page` first, then `seen_ids` | Optimizes for UI freshness; `insert_id` handles any duplicate sends |

## Race Condition Analysis

- **Poller writes while web reads:** Dict get/put are atomic per key — UI reads old or new value, never partial. Safe.
- **Overlapping poller runs:** Likely skipped by Modal (standard serverless pattern, not explicitly documented). Even if overlap occurs, worst case is a redundant Amplitude POST — `insert_id` deduplicates. Safe.
- **Poller crash between writes:** `front_page` written first (UI stays fresh). If `seen_ids` fails to persist, next run re-sends to Amplitude — `insert_id` deduplicates. Safe.

## Files to Create/Modify

- **`app.py`** (new) — Entire application: Modal app, poller, FastAPI web app, HTML renderer, Reddit fetcher, Amplitude sender
- **`README.md`** (modify) — Deployment instructions and architecture overview
