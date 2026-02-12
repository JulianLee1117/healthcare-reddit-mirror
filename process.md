# Process Log

<!-- Phase-by-phase execution log for the healthcare-reddit-mirror project. For each phase, record: timestamp (YYYY-MM-DD HH:MM TZ), results of implementation and verification, rationale behind any choices or deviations from PLAN.md. Keep it easy to read for mere humans.-->

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
