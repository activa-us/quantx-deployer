# Claude Code — Add Options Backtest Cache to main.py

## Goal
Wire the SQLite backtest cache (already built for equity) into the
`/api/options/backtest` SSE route.  Zero new dependencies — same
`get_backtest_cache` / `set_backtest_cache` functions already used by
the equity route.

---

## Step 1: Find the options backtest route

```bash
grep -n "options/backtest\|options_backtest\|run_options_backtest" api/main.py
```

You'll see something like:
```
XXXX: @app.get("/api/options/backtest")
XXXX: async def options_backtest_sse(request: Request, ...):
```

Note the exact line numbers.

---

## Step 2: View the full route function

```bash
sed -n 'STARTLINE,ENDLINE p' api/main.py
```

(Use the line range of the whole function — typically 30-60 lines.)

---

## Step 3: Apply this change

The function currently does something like:

```python
@app.get("/api/options/backtest")
async def options_backtest_sse(request: Request, ...params...):
    config = { ... }   # builds config dict from query params
    
    async def generate():
        for event in run_options_backtest_stream(config):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

Replace it with this pattern (adapt to match exact variable names found in Step 2):

```python
@app.get("/api/options/backtest")
async def options_backtest_sse(request: Request, ...params...):
    config = { ... }   # keep exactly as-is — do NOT change config construction

    # ── Cache check (non-streaming fast path) ─────────────────────────────
    from api.database import _options_bt_cache_key, get_backtest_cache, set_backtest_cache
    _cache_key = _options_bt_cache_key(config)
    _cached = get_backtest_cache(_cache_key, ttl_hours=24)
    if _cached is not None:
        # Return the full cached result as a single SSE stream:
        # start → trades → complete — all from cache, no compute
        _cached["_cached"] = True
        async def _cached_stream():
            trade_log = _cached.get("trade_log", [])
            metrics   = _cached.get("metrics", {})
            yield f"data: {json.dumps({'type': 'start', 'total': len(trade_log), 'cached': True})}\n\n"
            for trade in trade_log:
                yield f"data: {json.dumps({'type': 'trade', 'trade': trade})}\n\n"
            yield f"data: {json.dumps({'type': 'complete', 'metrics': metrics, 'trade_log': trade_log, '_cached': True})}\n\n"
        return StreamingResponse(_cached_stream(), media_type="text/event-stream")

    # ── Live compute path (existing code, unchanged) ───────────────────────
    _collected_trades: list = []
    _collected_metrics: dict = {}

    async def generate():
        nonlocal _collected_trades, _collected_metrics
        for event in run_options_backtest_stream(config):
            if await request.is_disconnected():
                break
            # Collect for cache storage
            if event.get("type") == "trade":
                _collected_trades.append(event["trade"])
            elif event.get("type") == "complete":
                _collected_metrics = event.get("metrics", {})
                # Store in cache after complete event
                set_backtest_cache(
                    _cache_key,
                    {"metrics": _collected_metrics, "trade_log": _collected_trades},
                    strategy=config.get("strategy_type", ""),
                    symbol=config.get("symbol", ""),
                )
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

**Key rules when applying:**
- Keep the EXACT config dict construction — do not change any variable names there
- The `nonlocal` keyword requires Python 3 — fine on Railway
- The cache import line should go at the top of the function (inside the function body), not at module level, to avoid circular import risk
- If `run_options_backtest_stream` is already imported at module level, don't import it again

---

## Step 4: Verify

```bash
grep -n "_options_bt_cache_key\|set_backtest_cache\|_cached_stream\|_cache_key" api/main.py | head -20
```

Should show 4-6 lines all within the options_backtest_sse function.

---

## Step 5: Also update `/api/options/cache/stats` route (if it exists)

```bash
grep -n "cache/stats\|cache_stats" api/main.py
```

If it exists, no change needed — it already queries `backtest_cache` table which now stores both equity and options results.

---

## Notes
- `database.py` has already been updated with `_options_bt_cache_key()` — commit that first
- TTL is 24h (same as equity cache)
- The ⚡ badge in the frontend reads `event._cached === true` from the `complete` event — this is already wired in index.html from last session
- No schema changes needed — options results go into the same `backtest_cache` table
