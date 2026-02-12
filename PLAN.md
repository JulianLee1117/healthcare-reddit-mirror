# Healthcare Reddit Mirror — Implementation Plan

## Context

Build and deploy a web app on Modal that continuously polls r/healthcare, sends new-post events to Amplitude, and displays current front-page posts in a web UI. The code must be correct, race-free, maintainable, concise, and secure.

**Architecture:** Single `app.py` file deployed on Modal with two functions:
1. A **scheduled poller** (every 5 min) that fetches Reddit RSS, detects new posts, sends events to Amplitude, and caches posts in Modal Dict
2. An **ASGI web app** (FastAPI) that reads cached posts from Modal Dict and renders server-side HTML

**Why RSS instead of JSON/OAuth:**
- Reddit blocks `.json` requests from cloud IPs (verified: returns 403 from Modal)
- Reddit RSS (`/r/healthcare.rss`) returns 200 from Modal — no auth needed
- RSS (Atom feed) contains all fields the assessment requires: `id`, `title`, `link`, `author`
- Eliminates OAuth credential management, token refresh, and the `reddit-secret` — simpler code, fewer failure modes
- Uses stdlib `xml.etree.ElementTree` for parsing — no extra dependencies

**Why this architecture:**
- Caching posts in Modal Dict keeps the web UI fast (<10ms) and avoids burning Reddit rate limits on page loads
- `modal.Dict` provides atomic key-level get/put — no file-locking issues like Volume would have
- `modal.Cron` likely skips overlapping invocations (standard serverless pattern). Even if it doesn't, Amplitude `insert_id` dedup prevents duplicate events
- Amplitude's `insert_id` dedup (7-day window) is the safety net if the poller crashes after sending events but before updating `seen_ids`

---

## Phase 1: Project Scaffolding & Modal Setup ✅

**Goal:** Get a minimal Modal app deployed with a "hello world" web endpoint.

**File:** `app.py`

Create the Modal app skeleton:
- `modal.App("healthcare-reddit-mirror")`
- `modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")` (httpx is included via `fastapi[standard]`)
- A minimal FastAPI app with `GET /` returning "Hello"
- Wire it up with `@modal.asgi_app()`

**Verify:** Run `modal serve app.py`, visit the printed URL, confirm "Hello" response.

---

## Phase 2: Reddit Polling via RSS

**Goal:** Fetch r/healthcare posts via RSS, parse the Atom feed, and store posts in Modal Dict.

**No credentials needed.** Reddit RSS is unauthenticated and reachable from Modal (verified: 200 OK, 25 entries).

**Add to `app.py`:**

1. Define `modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)`
2. Define constants: `RSS_URL = "https://www.reddit.com/r/healthcare.rss"`, `USER_AGENT`
3. Implement `_fetch_reddit() -> list[dict] | None`:
   - GET `RSS_URL` with `User-Agent` header via httpx
   - Parse response as Atom XML using `xml.etree.ElementTree`
   - Namespace: `{"atom": "http://www.w3.org/2005/Atom"}`
   - Extract per entry:
     - `id`: from `<id>t3_xxx</id>` — strip `t3_` prefix for bare post ID
     - `title`: from `<title>` text
     - `link`: from `<link href="...">` attribute — already a full Reddit URL
     - `author`: from `<author><name>/u/Username</name>` — strip `/u/` prefix
     - `created_utc`: from `<published>` ISO 8601 → parse to Unix timestamp
   - Catch both `httpx.HTTPError` and `xml.etree.ElementTree.ParseError` — return `None` on any error (log it, don't crash)
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
         "device_id": "reddit-mirror",              # required for insert_id dedup
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
   - HTML table with columns: title (as link), author
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

## Phase 4b: Enhancements (Above & Beyond)

**Goal:** Demonstrate growth engineering mindset with instrumentation depth and UI polish — all within the single-file, zero-dependency constraint.

**Enhancements:**

1. **Server-side page view tracking** — Send `mirror_page_viewed` event to Amplitude from `GET /` via FastAPI `BackgroundTasks` (non-blocking response). Shows holistic instrumentation thinking beyond just data ingestion.
   - Add `secrets=[modal.Secret.from_name("amplitude-secret")]` to `serve` function
   - Event: `device_id: "reddit-mirror-web"` (distinct from poller's `"reddit-mirror"`), `event_properties: {post_count}`
   - No `insert_id` needed (each page view is a unique event)
   - Graceful degradation: `os.environ.get()` + try/except — never crashes the page

2. **Richer Amplitude event properties** — Add to `reddit_post_ingested` events:
   - `post_age_minutes`: `round((now - created_utc) / 60)` — enables "how stale are posts at ingestion?" charts
   - `post_position`: 1-indexed rank in the RSS feed — enables "what rank are new posts?" analysis

3. **"Last updated" footer** — Store `time.time()` in Dict key `last_polled` during each poll cycle. Display as relative time in UI footer.

4. **Relative timestamps** — `_relative_time(unix_ts)` helper: "just now" / "5m ago" / "2h ago" / "3d ago". Pure Python, no dependencies.

5. **UI polish** — Card-style table with `border-radius`, `box-shadow`, hover states, subtle `#fafafa` background, subtitle text. Still f-string HTML.

6. **Content snippet from RSS** — Extract `<content>` HTML from Atom feed, strip tags via `re.sub`, truncate to 200 chars. Show as subtitle under each post title. Adds `content_length` to Amplitude event properties.

7. **Question detection** — `is_question: title.endswith("?")` added to Amplitude event properties. Enables "what fraction of posts are questions?" analysis.

**Verify:** `modal serve app.py` — check all enhancements render correctly. Check Amplitude for `mirror_page_viewed` events and enriched `reddit_post_ingested` properties.

---

## Phase 5: Deploy & End-to-End Verification ✅

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
| Reddit data source | RSS (Atom feed) at `/r/healthcare.rss` | `.json` returns 403 from cloud IPs; RSS returns 200, has all required fields (title, link, author, id), no credentials needed |
| State storage | `modal.Dict` | Atomic get/put, no file locking, simple for small structured data |
| Polling frequency | Every 5 min (`modal.Cron`) | r/healthcare is low-traffic; avoids rate limits; Cron doesn't drift on redeploy |
| Web UI data source | Read from Dict cache | Sub-10ms response, no Reddit API call per page load |
| HTTP client | `httpx` (bundled with `fastapi[standard]`) | No extra dependency; async-capable; actively maintained |
| XML parser | `xml.etree.ElementTree` (stdlib) | No extra dependency; sufficient for well-formed Atom feed |
| Template engine | f-string with `html.escape()` | Stdlib XSS protection; no Jinja2 dependency for ~30 lines of HTML |
| Dedup strategy | `seen_ids` set in Dict + Amplitude `insert_id` | Belt and suspenders — Dict is primary, `insert_id` is crash-recovery safety net |
| `user_id` for Amplitude | `"reddit-mirror"` | System-generated events; satisfies 5-char minimum |
| Write order in poller | `front_page` first, then `seen_ids` | Optimizes for UI freshness; `insert_id` handles any duplicate sends |

## Assessment Field Mapping (RSS → Requirements)

| Assessment Requirement | RSS Source | Transformation |
|---|---|---|
| Post `id` (dedup key) | `<id>t3_1iwjv98</id>` | Strip `t3_` prefix → `1iwjv98` |
| `title` (Amplitude + UI) | `<title>` text | None (use as-is) |
| `link` (Amplitude + UI) | `<link href="https://...">` | Read `href` attribute (already full URL) |
| `author` (Amplitude + UI) | `<author><name>/u/Foo</name>` | Strip `/u/` prefix → `Foo` |
| Timestamp (Amplitude `time`) | `<published>2026-02-12T21:02:11+00:00</published>` | Parse ISO 8601 → epoch milliseconds |

## Race Condition Analysis

- **Poller writes while web reads:** Dict get/put are atomic per key — UI reads old or new value, never partial. Safe.
- **Overlapping poller runs:** Likely skipped by Modal (standard serverless pattern, not explicitly documented). Even if overlap occurs, worst case is a redundant Amplitude POST — `insert_id` deduplicates. Safe.
- **Poller crash between writes:** `front_page` written first (UI stays fresh). If `seen_ids` fails to persist, next run re-sends to Amplitude — `insert_id` deduplicates. Safe.

## Files to Create/Modify

- **`app.py`** (new) — Entire application: Modal app, poller, FastAPI web app, HTML renderer, RSS fetcher, Amplitude sender
- **`README.md`** (modify) — Deployment instructions and architecture overview
