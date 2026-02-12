# Process Log

<!-- Phase-by-phase execution log for the healthcare-reddit-mirror project. For each phase, record the REAL time: timestamp (YYYY-MM-DD HH:MM TZ), results of implementation and verification, rationale behind any choices or deviations from PLAN.md. Keep it easy to read for mere humans.-->

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
