# Healthcare Reddit Mirror — Implementation Plan

## Context

Build and deploy a web app on Modal that continuously polls r/healthcare, sends new-post events to Amplitude, and displays current front-page posts in a web UI. The code must be correct, race-free, maintainable, concise, and secure.

**Architecture:** `app.py` backend deployed on Modal + React SPA frontend:
1. A **scheduled poller** (every 5 min) that fetches Reddit RSS (both `/hot` and `/new`), detects new posts, sends events to Amplitude, and caches posts in Modal Dict
2. A **FastAPI API** serving a JSON endpoint (`/api/posts`) from Dict cache
3. A **React SPA** (served as static files from FastAPI) that renders the UI

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
- `modal.App("reddit-mirror")`
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

## Phase 4: API Endpoints ✅ (originally server-side HTML, now JSON API)

**Goal:** Expose cached data as JSON API endpoint for the React frontend.

**Endpoints in `app.py`:**

1. `GET /api/posts` — Returns front-page posts from Dict cache:
   ```json
   {
     "posts": [{"id", "title", "link", "author", "created_utc", "content"}],
     "last_polled": 1707753600.0
   }
   ```
2. `GET /healthz` → `{"status": "ok"}`
3. `GET /` — Serves the React SPA (`index.html` from built static files)
4. `@modal.concurrent(max_inputs=100)` on the serve function

**CORS:** Add `CORSMiddleware` for local dev (`modal serve` + Vite dev server on different ports).

**Verify:** `curl /api/posts` returns valid JSON with all required fields.

---

## Phase 4b: Amplitude Enrichments ✅

**Enrichments applied to `reddit_post_ingested` events:**

1. **`post_age_minutes`** — `round((now - created_utc) / 60)` — how stale are posts at ingestion?
2. **`post_position`** — 1-indexed rank in the RSS feed
3. **`content_length`** — length of content snippet
4. **`is_question`** — `"?" in title` (catches questions with parenthetical suffixes like `"Is it normal...? (27M)"`)

~~`feed_source`~~ — Removed. In steady state, genuinely new posts are almost always first seen via `/new`, making this field uninformative.

**Poller also stores:**
- `last_polled`: `time.time()` — exposed in `/api/posts` response
- `content`: stripped/cleaned text snippet per post — exposed in `/api/posts` response

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

## Phase 6: Ingestion & Dashboard

**Goal:** Ensure every new r/healthcare post is captured (not just front-page posts) and set up an Amplitude dashboard.

### 6a: Dual-Feed Polling (Hot + New)

**Problem:** The assessment says "for each new post (by `id`)" and Naomi confirmed "syndicate new posts." We only poll `/hot`. Reddit's hot algorithm can bury posts with zero engagement — they never appear on the front page. Verified: `/new.rss` returns posts that `/hot.rss` doesn't.

**Solution:** Poll both feeds in `poll_reddit()`:
- `/r/healthcare/new.rss` → comprehensive ingestion (Amplitude events for **every** post)
- `/r/healthcare.rss` (hot) → UI display (shows actual front page as assessment requires)

**Implementation:**
1. Add `RSS_NEW_URL = "https://www.reddit.com/r/healthcare/new.rss"` constant
2. Parameterize `_fetch_reddit(url: str)` — accept URL as argument
3. In `poll_reddit()`:
   - Fetch both feeds: `hot_posts = _fetch_reddit(RSS_URL)`, `new_posts_raw = _fetch_reddit(RSS_NEW_URL)`
   - Merge for dedup: build `all_posts` dict keyed by ID from both feeds (hot takes precedence for duplicates — preserves hot-sort position)
   - Dedup against `seen_ids` using merged set
   - Write `front_page = hot_posts` (UI shows hot sort, per assessment requirement)
   - Send Amplitude events for all new-to-us posts from **either** feed
4. Same `seen_ids` set covers both feeds — no double-sending

**Verify:** `modal run app.py::poll_reddit` — check that posts from /new that aren't in /hot still get Amplitude events. UI should still show hot-sorted posts.

## Phase 7: React Frontend ✅

**Goal:** Replace the f-string server-side HTML with a React SPA.

**Architecture:**
- React SPA built with Vite (`frontend/` directory)
- Built static files served from FastAPI via `StaticFiles` mount (with `check_dir=False` since the directory only exists inside the Modal container)
- Modal image includes the pre-built `frontend/dist/` via `add_local_dir()`
- Fetches from `GET /api/posts`

**UI:** Editorial design — Instrument Serif + Outfit fonts, sage green accent (#2D6A4F), warm off-white background, staggered fade-in animations on page load. Posts list with title (linked), content snippet, author (linked to profile), and relative timestamp.

**Implementation:**
1. Created `frontend/` with Vite + React (package.json, vite.config.js, index.html, src/)
2. `App.jsx`: fetches `/api/posts`, renders posts list, auto-refreshes every 5 min
3. `App.css`: editorial aesthetic with CSS variables, responsive layout, CSS animations
4. Updated `app.py`:
   - Removed `_render_html()`, `_relative_time()` (moved to React)
   - Added `.add_local_dir("frontend/dist", remote_path="/frontend/dist")` to Modal image
   - Added `GET /api/posts` JSON endpoint
   - Mounted `StaticFiles` at `/assets` for JS/CSS bundles
   - `GET /` serves `index.html` via `FileResponse`

**Verify:** `modal serve app.py` → React app loads, 25 posts render from API, all links work, animations smooth.

---

## Phase 8: Amplitude Enrichments & Analytics

**Goal:** Enrich event data to unlock meaningful Amplitude charts for growth analysis. With minimal data (~25 posts, one event type, static `user_id`), charts would be thin. These enrichments transform raw event logging into actionable growth intelligence.

**Changes to `app.py`:**

1. **Topic categorization** — `_categorize_topic(title, content)` keyword classifier with 6 categories:
   - `insurance_billing`: cost, coverage, payment pain points
   - `policy_regulation`: FDA, CMS, legislation, political health policy
   - `health_tech`: AI, EHR, EMR, telehealth, digital health tools
   - `career_workforce`: jobs, burnout, documentation burden, certifications
   - `patient_experience`: diagnoses, prescriptions, screenings, medical records
   - `other`: default fallback
   Added as `topic` property on `reddit_post_ingested` events. Enables topic trend analysis — "40% of posts are billing questions" is a real growth insight for a health company.

2. **Author as `user_id`** — changed from static `"reddit-mirror"` to post author name. Unlocks Amplitude's user-level analytics: author frequency, new vs returning authors, per-author activity patterns. Same analytical framework a growth engineer applies to product users.

3. **`has_content` property** — boolean distinguishing text posts (with body) from link-only posts. Enables content strategy analysis (what ratio of community activity is discussion vs link sharing).

**Existing Amplitude data:** Old events remain with `user_id: "reddit-mirror"` and basic properties. New events get enriched schema going forward. Amplitude handles mixed schemas gracefully — no backfill needed. Amplitude dedupes via `insert_id` + `device_id`, so clearing `seen_ids` would NOT update old events (old insert_ids are already consumed).

~~**`reddit_poll_completed` event**~~ — Removed. Operational telemetry (poll cycle stats) doesn't belong in an analytics platform meant for user/content events. This is infrastructure monitoring, not growth analytics — it belongs in application logs (Modal captures stdout natively). Keeping Amplitude focused on the single `reddit_post_ingested` event type shows discipline about what merits tracking.

**Verify:** `modal run app.py::poll_reddit` — check Amplitude for `topic` property on new events and author-level user entries.

---

## Key Design Decisions

| Decision | Choice | Why |
|---|---|---|
| Reddit data source | RSS (Atom feed): `/r/healthcare.rss` (hot) + `/r/healthcare/new.rss` | `.json` returns 403 from cloud IPs; dual-feed ensures comprehensive ingestion (/new) while UI shows actual front page (/hot) |
| State storage | `modal.Dict` | Atomic get/put, no file locking, simple for small structured data |
| Polling frequency | Every 5 min (`modal.Cron`) | r/healthcare is low-traffic; avoids rate limits; Cron doesn't drift on redeploy |
| Web UI data source | Read from Dict cache | Sub-10ms response, no Reddit API call per page load |
| HTTP client | `httpx` (bundled with `fastapi[standard]`) | No extra dependency; async-capable; actively maintained |
| XML parser | `xml.etree.ElementTree` (stdlib) | No extra dependency; sufficient for well-formed Atom feed |
| Frontend | React SPA (Vite) served from FastAPI `StaticFiles` | Interviewer recommended JS framework; they use React internally; enables richer UI (charts, routing) |
| Dedup strategy | `seen_ids` set in Dict + Amplitude `insert_id` | Belt and suspenders — Dict is primary, `insert_id` is crash-recovery safety net |
| `user_id` for Amplitude | Post author (was `"reddit-mirror"`) | Enables Amplitude user-level analytics (author frequency, retention) |
| Write order in poller | `front_page` first, then `seen_ids` | Optimizes for UI freshness; `insert_id` handles any duplicate sends |

## Data Store: Modal Dict Schema

**Why Modal Dict over an external database:**
- r/healthcare is ~5-10 posts/day. Total data is <1MB even after months.
- Access patterns are pure key-value: write in poller, read in API. No queries, joins, or transactions needed.
- Modal Dict provides atomic per-key get/put — sufficient for our read/write patterns.
- Zero config, zero cost, zero latency (same infrastructure as the app).
- Adding Postgres/SQLite would be over-engineering. The assessment says "if needed" — it's not.

**Dict: `healthcare-reddit-posts`**

| Key | Type | Written by | Read by | Description |
|---|---|---|---|---|
| `front_page` | `list[dict]` | `poll_reddit` | `GET /api/posts` | Current hot-sorted posts (~25). Each dict: `{id, title, link, author, created_utc, content}` |
| `seen_ids` | `set[str]` | `poll_reddit` | `poll_reddit` | All post IDs ever seen. Capped at 5000 (resets to current if exceeded). Primary dedup mechanism. |
| `last_polled` | `float` | `poll_reddit` | `GET /api/posts` | Unix timestamp of last successful poll. Displayed as "last updated" in UI. |

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

- **`app.py`** — Modal app: poller (dual-feed), FastAPI API endpoint, Amplitude sender
- **`frontend/`** — React SPA (Vite): posts page
- **`README.md`** — Deployment instructions, architecture overview, Amplitude dashboard screenshot
