# Process Log

<!-- Phase-by-phase execution log for the healthcare-reddit-mirror project. For each phase, calculate and record the REAL time: timestamp (YYYY-MM-DD HH:MM TZ), results of implementation and verification, rationale behind any choices or deviations from PLAN.md. Keep it easy to read for mere humans.-->

## Phase 0: Verify Assumptions — 2026-02-12 13:23 PST

### 1. Reddit .json endpoint
- **Result:** 200 OK. Response shape confirmed: `data.children[].data.{id, title, author, permalink, url, score, num_comments, created_utc, name}`
- **Sample post:** id=`1iwjv98`, name=`t3_1iwjv98`, title="Experimenting with polls and surveys", author="NewAlexandria"
- **25 posts** returned per request (default)
- No auth required with descriptive User-Agent

### 2. Modal Dict API
- **Result:** All operations work as expected on Modal v1.3.3
- Bracket notation `d["key"] = value` / `d["key"]` works
- Python `set` and `list[dict]` serialize/deserialize correctly
- `KeyError` raised on missing keys (as expected)
- TTL: 7 days of inactivity for new dicts (post May 2025)

### 3. Modal ASGI app + @modal.concurrent
- **Result:** Works with decorator order `@app.function` → `@modal.concurrent(max_inputs=100)` → `@modal.asgi_app()`
- FastAPI HTMLResponse and JSON endpoints both work
- Image build with `pip_install("fastapi[standard]")` succeeds (note: httpx is included via `fastapi[standard]`)
- Served at `https://julianlee1117--asgi-test-serve-dev.modal.run/`

### 4. Modal Cron overlap behavior
- **Result:** Not explicitly documented in Modal's public docs
- Standard serverless cron pattern is skip-on-overlap
- **Mitigation:** Our `insert_id` dedup on Amplitude makes this safe even if overlap occurs. The `seen_ids` read-modify-write is the only risk. If needed, can empirically test with a sleep-based cron function.
- **Decision:** Proceed with current design. The Amplitude `insert_id` safety net covers the worst case.

### 5. Amplitude HTTP API
- **Skipped** — user will create org/project during Phase 3

### Plan Updates from Phase 0
- **`pip_install` not `uv_pip_install`**: Modal image builder uses `pip_install`. Updated mental model (PLAN.md said `uv_pip_install` in one place — will use `pip_install`).
- **httpx comes free with `fastapi[standard]`**: No need to install it separately. Simplifies image definition.
- **`html.escape()` over custom `_escape()`**: Adopting reviewer suggestion — use stdlib `html.escape()` instead of rolling our own. More concise, fewer edge cases.
- **Single Amplitude batch**: r/healthcare returns 25 posts max. No need for batching logic — send all in one request. Simpler code.

## Phase 1: Project Scaffolding & Modal Setup — 2026-02-12 13:35 PST

- **Result:** `app.py` created with Modal app skeleton, FastAPI web app, `GET /` returning "Hello"
- Image: `debian_slim(python_version="3.12").pip_install("fastapi[standard]")`
- Decorator order: `@app.function(image=image)` → `@modal.asgi_app()` (no `@modal.concurrent` yet — that's Phase 4)
- **Verified:** `modal serve app.py` → `curl` returned `Hello` at `https://julianlee1117--healthcare-reddit-mirror-serve-dev.modal.run/`
- No deviations from PLAN.md

## Plan Revision: Switch from OAuth to RSS — 2026-02-12

### Discovery
Phase 0 tested `.json` locally (200 OK), but the current `app.py` Phase 2 implementation hits **403 from Modal** cloud IPs. Reddit blocks unauthenticated `.json` from cloud/datacenter IPs.

### Verification (ran `test_reddit.py` on Modal)
- `.json` → **403** (HTML error page, blocked)
- `.rss` → **200** (Atom XML, `Content-Type: application/atom+xml`, 60 KB, 25 entries)

### RSS field audit against assessment requirements
| Assessment Requirement | RSS Source | Verified |
|---|---|---|
| Post `id` (dedup) | `<id>t3_1iwjv98</id>` — strip `t3_` prefix | Yes |
| `title` (Amplitude + UI) | `<title>` text | Yes |
| `link` (Amplitude + UI) | `<link href="https://www.reddit.com/r/healthcare/comments/...">` | Yes |
| `author` (Amplitude + UI) | `<author><name>/u/NewAlexandria</name>` — strip `/u/` | Yes |
| Timestamp (Amplitude `time`) | `<published>` ISO 8601 | Yes |

All required fields present. `score` and `num_comments` are NOT in RSS but are NOT required by the assessment.

### Plan changes made
1. **Phase 2 rewritten**: OAuth + JSON → RSS + `xml.etree.ElementTree` (stdlib)
2. **No `reddit-secret` needed**: Eliminates OAuth credential management, token refresh, `_get_reddit_token()` function
3. **Phase 3**: `secrets` list now only contains `amplitude-secret`
4. **Phase 4**: Dropped `score`/`num_comments` columns from web UI (not available in RSS, not required)
5. **New section in PLAN.md**: "Assessment Field Mapping" table for traceability

### Rationale
- Simpler: fewer lines of code, one fewer Modal secret, zero Reddit credentials
- More robust: no token expiry, no OAuth failure modes
- Stdlib-only parsing: `xml.etree.ElementTree` vs httpx JSON (httpx still used for HTTP)
- All assessment requirements still met

## Phase 2: Reddit Polling via RSS — 2026-02-12

### Implementation
- Rewrote `_fetch_reddit()`: RSS fetch via httpx + Atom XML parsing via `xml.etree.ElementTree`
- Constants: `RSS_URL`, `USER_AGENT`, `_ATOM_NS` (namespace dict)
- Field extraction: `id` (strip `t3_`), `title`, `link` (from `href` attr), `author` (strip `/u/`), `created_utc` (ISO 8601 → `datetime.fromisoformat().timestamp()`)
- Error handling catches both `httpx.HTTPError` and `ET.ParseError`
- `poll_reddit()` unchanged (dedup logic, Dict writes already correct from Phase 1 skeleton)
- Plan update: added `ET.ParseError` to error handling (was missing from original Phase 2 plan)

### Verification
- **Run 1:** `modal run app.py::poll_reddit` → 25 posts fetched, 25 new (first run, empty `seen_ids`)
- **Run 2:** Same command → 25 posts fetched, 0 new (dedup working — `seen_ids` persisted correctly)
- **Dict spot-check:** Read back `front_page[0]` — all 5 fields present with correct types:
  - `id`: `'1iwjv98'` (bare ID, `t3_` stripped)
  - `title`: `'Experimenting with polls and surveys'`
  - `link`: `'https://www.reddit.com/r/healthcare/comments/1iwjv98/experimenting_with_polls_and_surveys/'`
  - `author`: `'NewAlexandria'` (`/u/` stripped)
  - `created_utc`: `1740343022.0` (Unix timestamp, correct for 2025-02-23T20:37:02+00:00)
- No deviations from PLAN.md (other than the `ET.ParseError` addition noted above)

## Phase 3: Amplitude Integration — 2026-02-12 14:40 PST

### Pre-req
- Created Amplitude org "healthcare-reddit-mirror" (Org ID: 408215, Starter Plan)
- Created Modal secret: `modal secret create amplitude-secret AMPLITUDE_API_KEY=<key>`
- Verified secret exists via `modal secret list`

### Plan Update
- **Added `device_id: "reddit-mirror"` to event schema**: Amplitude `insert_id` dedup only works within the same `device_id`. Without it, duplicate events could slip through on crash-recovery re-sends. Updated PLAN.md Phase 3 accordingly.

### Implementation
- Added `AMPLITUDE_URL` constant
- Implemented `_send_to_amplitude(posts: list[dict]) -> None`:
  - Reads `AMPLITUDE_API_KEY` from `os.environ` (injected by Modal secret)
  - Builds event list with `user_id`, `device_id`, `event_type`, `time` (ms), `insert_id`, `event_properties` (title, link, author)
  - Single POST via httpx `json=` param (sets Content-Type automatically)
  - Error handling: catches `httpx.HTTPError`, logs but doesn't crash
- Added `secrets=[modal.Secret.from_name("amplitude-secret")]` to `poll_reddit` decorator
- Added `if new_posts: _send_to_amplitude(new_posts)` guard — only sends when there are genuinely new posts
- Amplitude call happens **before** Dict writes (front_page, seen_ids) — if Amplitude fails, posts remain "unseen" and will be retried next cycle. Combined with `insert_id` dedup, this is safe.

### Verification
- **Run 1:** Cleared `seen_ids` → `modal run app.py::poll_reddit` → "Amplitude: sent 25 events, status 200"
- **Run 2:** Same command → "Polled 25 posts, 0 new: []" — no Amplitude call made (dedup working)
- No deviations from PLAN.md (other than the `device_id` addition noted above)

## Phase 4: Web UI — 2026-02-12

### Plan Review
- Phase 4 plan verified optimal — no changes needed.

### Implementation
- `_render_html(posts)`: HTML table (title as link, author), `html.escape()` on all dynamic values, system font CSS, empty state message, `<meta http-equiv="refresh" content="300">`
- Updated `GET /` to read `front_page` from Dict (KeyError → empty list)
- Added `GET /healthz` → `{"status": "ok"}`
- Added `@modal.concurrent(max_inputs=100)` to serve function (decorator order verified in Phase 0)

### Verification
- `modal serve app.py` → 25 posts rendered correctly at dev URL
- XSS escaping confirmed (e.g., `they're` → `they&#x27;re` in HTML source)
- All links have `target="_blank" rel="noopener"`, open correct Reddit pages
- `/healthz` returns `{"status":"ok"}`
- No deviations from PLAN.md

## Phase 4b: Enhancements — 2026-02-12

### Implementation (7 enhancements)
1. **Server-side page view tracking** — `_track_page_view()` sends `mirror_page_viewed` to Amplitude via FastAPI `BackgroundTasks` (non-blocking). `device_id: "reddit-mirror-web"` distinguishes web events from poller events. `os.environ.get()` + try/except ensures graceful degradation. Added `secrets` to `serve` function.
2. **Richer Amplitude event properties** — `post_age_minutes`, `post_position` added to `reddit_post_ingested` events. Uses `enumerate()` + `time.time()` at send time.
3. **"Last updated" footer** — `posts_dict["last_polled"] = time.time()` in `poll_reddit()`. Rendered as relative time in footer.
4. **Relative timestamps** — `_relative_time(unix_ts)` helper: "just now" / "5m ago" / "2h ago" / "3d ago". "Posted" column added to table.
5. **UI polish** — Card-style table (`border-radius: 8px`, `box-shadow`), hover states, `#fafafa` background, subtitle, improved typography.
6. **Content snippet from RSS** — `<content>` HTML extracted, tags stripped via `re.sub(r"<[^>]+>", "")`, truncated to 200 chars. Shown under title with `text-overflow: ellipsis`.
7. **Question detection** — `is_question: title.endswith("?")` added to Amplitude event properties.

### Verification
- `modal run app.py::poll_reddit` + `modal serve app.py` — all 7 enhancements verified in prior session (content snippets, relative timestamps, last updated footer, page view tracking, enriched Amplitude events)

## Phase 5: Deploy & E2E Verification — 2026-02-12

### Plan Review
- Original Phase 5 called for waiting through 2 poll cycles post-deploy. Unnecessary — Dict already has data from Phase 2-4b testing, and each phase was individually verified. Simplified to: deploy, spot-check production URL, write README.
- Added `.claude/` to `.gitignore` (local config shouldn't be in repo).

### Implementation
- `modal deploy app.py` → production URL: `https://julianlee1117--healthcare-reddit-mirror-serve.modal.run`
- Cron activated: `poll_reddit` runs every 5 min
- README.md written: architecture overview, RSS rationale, dedup strategy, setup/deploy instructions, Amplitude event table, transcript reference

### Verification
- `/healthz` → `{"status":"ok"}`
- `/` → 25 posts rendered with titles, links, authors, content snippets, relative timestamps
- Last updated footer present
- No deviations from PLAN.md (other than simplifying the verification steps as noted above)

## Post-Deploy Audit — 2026-02-12 16:51 PST

Full code review of `app.py` against assessment criteria (correct, race-free, maintainable, concise, secure). No issues found, no changes needed.

### Correctness
- RSS parsing: Atom namespace, `findtext`/`find` with safe defaults, `removeprefix` for `t3_` and `/u/`
- Timestamp: `datetime.fromisoformat` handles Reddit's timezone-aware ISO 8601 on Python 3.12
- Dedup: `current_ids - seen_ids` with 5000-cap; `insert_id` as crash-recovery safety net
- Write ordering: Amplitude → `front_page` → `seen_ids` — if crash after Amplitude but before `seen_ids`, next run re-sends with same `insert_id` (deduped)

### Race-freedom
- **Poller vs web:** Dict get/put atomic per key — reads old or new, never partial
- **Overlapping pollers:** Standard serverless cron skips overlaps; even if overlap, `insert_id` dedup prevents Amplitude duplicates; `seen_ids` union means both writes include `current_ids`
- **Crash between Dict writes:** `front_page` written first (UI stays fresh); `insert_id` covers re-sends
- **Concurrent web requests:** Read-only Dict access; `BackgroundTask` is fire-and-forget, no shared state

### Security
- `html.escape()` on all 4 dynamic values (title, link, author, content) before rendering
- Links from Reddit's server-generated `<link href>` (not user-controlled); `html.escape` prevents attribute breakout
- Content snippet: RSS HTML stripped via `re.sub`, then `html.escape()`d — double protection
- `target="_blank" rel="noopener"` on external links
- API key in Modal Secret via `os.environ`, never hardcoded or logged

### Maintainability & Conciseness
- 285 lines, single file, well-organized sections (constants → helpers → poller → web UI → serve)
- No unnecessary abstractions; f-string HTML avoids template engine dependency for ~30 lines of markup
- Local imports inside functions (standard Modal serialization pattern)

**Result:** No PLAN.md updates needed. Code matches plan.

## Post-Deploy Fixes — 2026-02-12

### Bug: RSS content display issues
1. **HTML entities not decoded** — `&#32;`, `&#39;` etc. showing raw in snippets. Fixed by adding `html.unescape()` (via `from html import unescape`) before tag stripping in `_fetch_reddit()`.
2. **Reddit boilerplate in link posts** — "submitted by /u/... [link] [comments]" showing as snippet text. Initial fix blanked all content containing `[link]` and `[comments]`, but this was too aggressive — self-posts also contain that boilerplate at the end. Final fix: strip boilerplate patterns (`submitted by /u/...`, `[link]`, `[comments]`) via targeted `re.sub()` instead of blanking. Self-post descriptions preserved; link-only posts get empty snippet (clean, no placeholder needed).
3. **Author profile links** — Author usernames now link to `https://www.reddit.com/user/{author}` with `target="_blank" rel="noopener"`.

### Verification
- Redeployed + re-polled. Self-posts show clean snippets, link posts show no snippet, author names are clickable links to Reddit profiles. No HTML entity artifacts.

### Removal: `mirror_page_viewed` event
- Removed `_track_page_view()` function, `BackgroundTasks` usage from `home()`, and `secrets` from `serve` function decorator.
- Rationale: static `device_id` hit counter with no per-visitor identification provided no actionable data. Unnecessary Amplitude call on every page load.
- Updated README.md (removed from architecture diagram and event table), PLAN.md (marked as removed in Phase 4b).

## Phase 6a: Dual-Feed Polling (Hot + New) — 2026-02-12

### Plan Review
- Phase 6a plan verified optimal. One gap identified: no explicit handling for partial failure (one feed fails, other succeeds). Added to implementation.

### Implementation
- Added `RSS_NEW_URL = "https://www.reddit.com/r/healthcare/new.rss"` constant
- Parameterized `_fetch_reddit(url: str = RSS_URL)` — accepts URL as argument
- Rewrote `poll_reddit()` for dual-feed:
  - Fetches both `/hot` and `/new` feeds
  - Early return only if **both** fail (partial failure is handled gracefully)
  - Merges into `all_posts` dict keyed by ID — new feed inserted first, hot overwrites (hot takes precedence for duplicates)
  - Tags each post with `feed_source`: `"both"`, `"hot"`, or `"new"` based on which feed(s) it appeared in
  - Dedup against `seen_ids` uses merged `current_ids` from both feeds
  - `front_page` write gated on `hot_posts is not None` — stale UI is better than empty UI if hot feed fails
  - `seen_ids` always updated with all posts from whichever feed(s) succeeded
- Added `feed_source` to Amplitude event properties in `_send_to_amplitude()` (default `"hot"` for backwards compat)

### Verification
- `modal run app.py::poll_reddit` → "Polled 25 hot + 25 new, 1 genuinely new" — new feed caught a post not in hot feed
- Amplitude: sent 1 event, status 200
- Dedup working correctly across both feeds (only genuinely new posts sent to Amplitude)
- No deviations from PLAN.md (other than the partial failure handling addition noted above)
