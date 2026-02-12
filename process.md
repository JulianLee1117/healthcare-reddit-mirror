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
