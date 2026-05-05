# Modal.com Integration Plan — Options Backtest Speed

## What Modal does
Serverless compute platform. You pay per second of CPU.
We route large backtests to Modal workers instead of Railway.

## Architecture

```
Student clicks "Run Backtest" (options, >30 days period)
  → Railway receives request
  → Railway checks SQLite cache → HIT: return instantly ⚡
  → MISS: check if options or equity?
      Equity (fast on Railway): run locally
      Options (slow): POST to Modal endpoint
          → Modal spins up N workers in parallel
          → Each worker handles 1 month of data
          → Results merged and returned
          → Stored in SQLite cache for next time
  → Return result to student
```

## Speed improvement
Current options backtest (1Y, SPY):
  - First run: ~60-90 seconds (sequential day-by-day)
  
With Modal (parallel months, 12 workers):
  - First run: ~5-8 seconds
  - Subsequent runs: ~50ms (SQLite cache hit)

## What needs building (next session)

### File 1: scripts/precompute_modal.py (new)
Modal app definition:
- @modal.function that runs options backtest for one month
- @modal.App with shared R2 parquet volume
- Orchestrator that splits date range → dispatches workers → merges

### File 2: api/main.py changes
- New route /api/options/backtest-fast that uses Modal
- Fallback to synchronous if Modal unavailable
- Cache result after Modal returns

### File 3: index.html
- Options Studio: route to /backtest-fast for long periods
- Keep synchronous for <30 day periods (faster without overhead)

## Modal credentials needed
- MODAL_TOKEN_ID (env var on Railway)
- MODAL_TOKEN_SECRET (env var on Railway)

## Cost estimate
- 1Y backtest: 12 workers × 5s each = 60 CPU-seconds
- At $0.000164/s = $0.01 per backtest
- 114 students × 10 backtests = $11.40 per class
- With caching: most students hit cache → ~$2-3 total per class

## Files to drop at start of next Modal session
- api/main.py (current)
- scripts/precompute_modal.py (if exists)
- api/options_backtest.py (to understand current flow)
- api/options_data.py (to understand R2 data access)
