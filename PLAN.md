# Healthcare Reddit Mirror — Implementation Plan

## Context

Build and deploy a web app on Modal that continuously polls r/healthcare, sends new-post events to Amplitude, and displays current front-page posts in a web UI. The code must be correct, race-free, maintainable, concise, and secure.

**Architecture:** `app.py` backend deployed on Modal + React SPA frontend:
1. A **scheduled poller** (every 5 min) that fetches Reddit RSS (both `/hot` and `/new`), detects new posts, sends events to Amplitude, and caches posts in Modal Dict
2. A **FastAPI API** serving JSON endpoints (`/api/posts`, `/api/insights`) from Dict cache
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

## Phase 4: API Endpoints ✅ (originally server-side HTML, now JSON API)

**Goal:** Expose cached data as JSON API endpoints for the React frontend.

**Endpoints in `app.py`:**

1. `GET /api/posts` — Returns front-page posts from Dict cache:
   ```json
   {
     "posts": [{"id", "title", "link", "author", "created_utc", "content"}],
     "last_polled": 1707753600.0
   }
   ```
2. `GET /api/insights` — Returns aggregated analytics from Dict (conditional on Phase 6e):
   ```json
   {
     "topic_counts": {"...": 12, ...},
     "author_counts": {"user1": 5, ...},
     "question_ratio": 0.32,
     "total_posts_seen": 150
   }
   ```
3. `GET /healthz` → `{"status": "ok"}`
4. `GET /` — Serves the React SPA (`index.html` from built static files)
5. `@modal.concurrent(max_inputs=100)` on the serve function

**CORS:** Add `CORSMiddleware` for local dev (`modal serve` + Vite dev server on different ports).

**Verify:** `curl /api/posts` returns valid JSON with all required fields.

---

## Phase 4b: Amplitude Enrichments ✅

**Enrichments applied to `reddit_post_ingested` events:**

1. **`post_age_minutes`** — `round((now - created_utc) / 60)` — how stale are posts at ingestion?
2. **`post_position`** — 1-indexed rank in the RSS feed
3. **`content_length`** — length of content snippet
4. **`is_question`** — `title.rstrip().endswith("?")`
5. **`feed_source`** — `"hot"`, `"new"`, or `"both"` (added in Phase 6a)
6. **`topic`** — auto-classified healthcare topic (added in Phase 6e, conditional on data analysis in 6d)

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

## Phase 6: Comprehensive Ingestion & Growth Enhancements

**Goal:** Ensure every new r/healthcare post is captured (not just front-page posts), backfill historical data, and add data-driven growth features.

**Execution order:** 6a (dual-feed) → 6b (Amplitude reset) → 6c (backfill) → 6d (analyze data & design classification) → 6e (implement classification + insights) → 6f (Amplitude dashboard). This order is intentional: we collect real data before making classification decisions.

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
   - Add `feed_source` to Amplitude event properties: `"hot"`, `"new"`, or `"both"` — enables analysis of which feed catches posts first
4. Same `seen_ids` set covers both feeds — no double-sending

**Verify:** `modal run app.py::poll_reddit` — check that posts from /new that aren't in /hot still get Amplitude events. UI should still show hot-sorted posts.

### 6b: Amplitude Clean Slate

**Problem:** Amplitude events are immutable — once sent, you cannot update their properties. We already have ~25 events from Phase 2-4 testing that lack enrichments (topic, feed_source, etc.). Since `insert_id` dedup prevents re-sending, these old events would be permanently incomplete.

**Solution:** Create a fresh Amplitude project within the same org (or delete events in the current project). Update the Modal secret with the new API key. This way all events — backfill and ongoing — have a consistent, complete schema.

**Why this is safe:** It's a brand new assessment org with only ~25 test events. Zero cost to reset.

### 6c: Historical Backfill

**Goal:** Prepopulate Amplitude with historical r/healthcare posts. Solves the cold-start problem — a growth tool that launches with an empty dashboard is useless.

**How RSS pagination works:** Reddit RSS supports `?after=t3_XXXXX` to paginate backward through posts. With `?limit=100`, each page returns up to 100 entries. Reddit caps total pagination at ~1000 posts. For r/healthcare (~5-10 posts/day), this covers roughly 3-6 months of history.

**Implementation:**
1. Add `backfill(pages: int = 10)` function with `@app.function` decorator (no cron — manual one-shot):
   ```python
   @app.function(image=image, secrets=[modal.Secret.from_name("amplitude-secret")])
   def backfill(pages: int = 10):
   ```
2. Paginate through `/r/healthcare/new.rss?limit=100&after=t3_LASTID`:
   - Fetch page → extract posts → get last post ID (`t3_` prefixed) → fetch next page
   - `time.sleep(2)` between requests (Reddit rate limit: ~10 req/min for unauthenticated RSS)
   - Stop when empty page or `pages` limit reached
   - Use `?limit=100` to maximize posts per page (10 pages × 100 = up to 1000 posts)
3. Process all collected posts:
   - Dedup against `seen_ids` (skip already-seen posts)
   - Send to Amplitude (with correct `created_utc` timestamps; `insert_id` makes reruns safe)
   - Update `seen_ids`
4. Does NOT overwrite `front_page` — that stays as current hot-sort posts from regular polling
5. Does NOT classify topics or update aggregates yet — that comes after 6d

**Note on `post_position`:** For backfilled posts, `post_position` reflects position within the /new pagination, not hot ranking. This is fine — it's still useful as chronological order.

**Safety:**
- `insert_id = f"reddit-{post_id}"` — Amplitude dedup means running backfill twice is harmless
- `seen_ids` dedup prevents double-processing
- No cron — only runs when manually invoked via `modal run app.py::backfill`

**Verify:** `modal run app.py::backfill` → check Amplitude for historical events with old timestamps. Check that `seen_ids` grew.

### 6d: Analyze Data & Design Classification (Manual Step)

**Goal:** Examine the backfilled data to design topic categories that actually fit r/healthcare's content. This is intentionally a manual analysis step — not code.

**Why defer:** We analyzed 27 current posts against the originally proposed keyword categories. Results:
- **48-52% of posts fell into "other"** — the taxonomy doesn't describe what r/healthcare actually contains
- Missing topics: health IT/EHR, pharmacy/medication access, medical research, patient rights
- Keyword overlap: "hospital", "doctor", "insurance" trigger wrong categories depending on context
- The subreddit is less consumer-oriented than assumed — significant health IT professional, policy analyst, and healthcare worker presence

**Process:**
1. After backfill, export or browse Amplitude events to see ~1000 post titles
2. Manually identify natural topic clusters in the actual data
3. Design categories with minimal "other" bucket (<20% target)
4. Choose keywords per category that minimize cross-contamination
5. Document the final taxonomy before implementing

**Decision point:** After seeing the data, we may decide:
- Topic classification adds real value → implement in 6e
- Topics are too noisy/overlapping → skip classification, keep insights simpler (just volume, question ratio, top authors)
- A different enrichment is more interesting (e.g., sentiment, post type: question vs news vs discussion)

### 6e: Classification & Insights (Conditional on 6d)

**Goal:** Implement topic classification and insights aggregation based on 6d findings.

**Implementation (specifics TBD after 6d):**
1. `_classify_topic(title: str, content: str) -> str` — keyword-based, categories from 6d analysis
2. Add `topic` to Amplitude event properties and post dicts
3. Update rolling aggregates in Dict during `poll_reddit()`:
   - `topic_counts`: `dict[str, int]`
   - `author_counts`: `dict[str, int]`
   - `total_posts_seen`: running total
   - `question_count`: posts ending with `?`
4. Expose via `GET /api/insights`
5. Re-run backfill to retroactively classify historical posts (requires Amplitude reset or new `insert_id` scheme)

**If classification is skipped:** Insights still include author_counts, question_count, total_posts_seen, post volume over time. These don't require topic classification and are still valuable.

### 6f: Amplitude Dashboard Setup

**Goal:** Configure 3-4 charts in Amplitude that tell a growth story about r/healthcare. Screenshot in README.

**Charts (core — always available):**
1. **Post volume over time** — `reddit_post_ingested` event count, daily granularity
2. **Question ratio trend** — `is_question = true` as % of total, over time
3. **Ingestion freshness** — average `post_age_minutes` over time (are we catching posts quickly?)

**Charts (conditional on 6e):**
4. **Topic distribution** — breakdown by `topic` event property (only if classification is implemented)

**Deliverable:** Screenshot of dashboard added to README under "## Amplitude Dashboard" section.

---

## Phase 7: React Frontend

**Goal:** Replace the f-string server-side HTML with React.

**Architecture:**
- React SPA built with Vite (`frontend/` directory)
- Built static files served from FastAPI via `StaticFiles` mount
- Modal image includes the pre-built `frontend/dist/` via `add_local_dir()`
- API calls to `/api/posts` and `/api/insights`

**Pages/Routes (React Router):**
1. **`/`** — Front page posts table (title as link, author, relative timestamp, content snippet, topic badge)
2. **`/insights`** — Analytics dashboard with topic distribution chart, top authors, question ratio, post velocity

**Implementation:**
1. `npm create vite@latest frontend -- --template react` (or similar)
2. Design UI using frontend-design skill
3. Fetch from `/api/posts` and `/api/insights` endpoints
4. Build: `cd frontend && npm run build`
5. Update `app.py`:
   - Remove `_render_html()`, `_relative_time()`, `_track_page_view()` (moved to React)
   - Add `app.add_local_dir("frontend/dist", remote_path="/frontend/dist")` to Modal image
   - Mount `StaticFiles(directory="/frontend/dist")` in FastAPI
   - Serve `index.html` as catch-all for client-side routing
6. Update Modal image if Node.js build step needed (or pre-build locally)

**Verify:** `modal serve app.py` → visit URL, confirm React app loads, posts render, `/insights` route works.

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
| `user_id` for Amplitude | `"reddit-mirror"` | System-generated events; satisfies 5-char minimum |
| Write order in poller | `front_page` first, then `seen_ids` | Optimizes for UI freshness; `insert_id` handles any duplicate sends |

## Data Store: Modal Dict Schema

**Why Modal Dict over an external database:**
- r/healthcare is ~5-10 posts/day. Even with full backfill, total data is ~3000 posts / <1MB.
- Access patterns are pure key-value: write aggregates in poller, read in API. No queries, joins, or transactions needed.
- Modal Dict provides atomic per-key get/put — sufficient for our read/write patterns.
- Zero config, zero cost, zero latency (same infrastructure as the app).
- Adding Postgres/SQLite would be over-engineering. The assessment says "if needed" — it's not.

**Dict: `healthcare-reddit-posts`**

**Core keys (always present):**

| Key | Type | Written by | Read by | Description |
|---|---|---|---|---|
| `front_page` | `list[dict]` | `poll_reddit` | `GET /api/posts` | Current hot-sorted posts (~25). Each dict: `{id, title, link, author, created_utc, content}` |
| `seen_ids` | `set[str]` | `poll_reddit`, `backfill` | `poll_reddit`, `backfill` | All post IDs ever seen. Capped at 5000 (resets to current if exceeded). Primary dedup mechanism. |
| `last_polled` | `float` | `poll_reddit` | `GET /api/posts` | Unix timestamp of last successful poll. Displayed as "last updated" in UI. |

**Insights keys (added in Phase 6e, conditional on 6d analysis):**

| Key | Type | Written by | Read by | Description |
|---|---|---|---|---|
| `topic_counts` | `dict[str, int]` | `poll_reddit`, `backfill` | `GET /api/insights` | Running totals per topic category (categories TBD from data analysis) |
| `author_counts` | `dict[str, int]` | `poll_reddit`, `backfill` | `GET /api/insights` | Running totals: `{"username": 5, ...}` |
| `total_posts_seen` | `int` | `poll_reddit`, `backfill` | `GET /api/insights` | Total unique posts ever ingested. |
| `question_count` | `int` | `poll_reddit`, `backfill` | `GET /api/insights` | Posts where title ends with `?`. |

**Size estimate:** All keys combined <100KB even after months of operation. Well within Dict limits.

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
- **Backfill concurrent with poller:** Both read-modify-write `seen_ids`. Worst case: a post is double-counted if both see it as "new" simultaneously. Amplitude deduplicates via `insert_id`. Mitigated by running backfill before enabling cron (deploy with cron disabled, run backfill, then redeploy with cron). Safe.
- **Aggregate counters (read-modify-write):** Not truly atomic across keys, but only one writer at a time in practice (poller in steady state, backfill as one-shot). Safe.

## Files to Create/Modify

- **`app.py`** — Modal app: poller (dual-feed), FastAPI API endpoints, Amplitude sender, topic classifier
- **`frontend/`** — React SPA (Vite): posts page, insights dashboard
- **`README.md`** — Deployment instructions, architecture overview, Amplitude dashboard screenshot
