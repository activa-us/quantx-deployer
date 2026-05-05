# QuantX Deployer — Handoff 05 May 2026 (Final)

## Session Commits (complete list)

| Commit | File(s) | Change |
|--------|---------|--------|
| `46cfcc4` | generate.py | Remove IBKR |
| `d5baab3` | generate.py | is_grid fix |
| `e300c68` | bot_template | GridState + DRY_RUN |
| `87e2c4d` | main.py | No silent fallback, dry_run |
| `3ffe696` | main.py | nuke-scripts endpoint |
| `82b683d` | bot_template | Remove __SIGNAL_FUNCTIONS_BLOCK__ |
| `fc6f9e1` | index.html | Trades display + DRY badge |
| `d50da9f` | bot_template | LongBridge fees + _processed_oids |
| backend CC | main.py, database.py | is_dry_run + clone endpoint |
| latest | index.html | Bot mode at creation + clone + live P/L |
| latest | bot_template | Fix OrderSide import (live orders) |
| latest | index.html | Clone mode choice |
| latest | index.html | Lots field in New Bot form |
| `f32b181` | main.py, database.py, bot_template | Duplicate process fix + LIMIT 500 + log dedup |
| latest | index.html | Live metrics + equity curve on dashboard |
| latest | main.py, database.py | Shared backtest result cache (Phase 1) |
| latest | index.html | ⚡ cached badge |
| latest | bot_template | datetime.utcnow() → timezone-aware |

---

## Confirmed Working ✅

- **Live orders on LongPort** (MSFT confirmed, BABA insufficient funds)
- **Dry run grid bot** (700.HK, full cycle with fees)
- **LongBridge fees**: buy 0.03%+stamp+SFC+HKEX, sell same minus stamp
- **Bot mode** (Dry/Live) baked in at creation
- **Clone bot** (OK=flip mode, Cancel=same mode)
- **Filter by mode** (All / Live / Dry Run / Off)
- **Lots field** in New Bot form with auto-fill from symbol API
- **Live metrics** from trades DB (Return, Win Rate, Max DD, Sharpe, Trades)
- **Equity curve** on bot dashboard (Chart.js from cumulative_pnl)
- **Shared backtest cache** — first student pays, everyone else instant
- **⚡ cached badge** on backtest results
- **No duplicate log lines** (f32b181 fix)
- **LIMIT 500** on trades query

---

## 9:30pm Test Plan

### Pre-test (do now):
1. Nuke scripts: `POST /api/admin/nuke-scripts {"pin":"quantx2025","email":"seanseahsg@gmail.com"}`
2. Deploy 700.HK dry run → confirm single log lines, fees in log
3. Run SYMMETRIC_GRID backtest on TQQQ.US 5Y → note time
4. Run same backtest again → should be <100ms with ⚡ badge

### At 9:30pm US market open:
5. Deploy TQQQ.US grid bot (Live, 1 lot, 4 levels, 0.5% spacing)
   → Check LongPort within 10s: 4 limit buy orders at ~$59.xx
6. Let one buy fill → confirm TP sell placed in LongPort + trade in dashboard
7. Deploy TURTLE_TRADER on TQQQ.US (Live, 1 lot)
   → Log should show: "Bar loop started", signals computing
   → Wait for bar close → confirm signal evaluation logged
8. Check equity curve populates after first fills

---

## Outstanding Issues

### Critical (fix before class)
1. **Duplicate trade records** — accepts for dry run (threads fire twice)
   Live trading should be single-fire (order fills are unique events)
2. **Top-5 metrics show live trades** — but if no trades yet, shows `—`
   Consider: show backtest metrics until first live trade, then switch
   
### Important (next session)
3. **Options backtest cache** — Tier 2 cache not yet applied to options
4. **Modal.com integration** — options backtest 60s → 5s
   Plan documented in MODAL_PLAN.md
5. **Startup prewarm** — pre-run top 20 equity combos at Railway boot
6. **quantx-central** — instructor dashboard still unreachable

### Nice to Have
7. Pause mode (stop process, leave orders open)
8. Export Strategy button
9. Signal bot live verification

---

## Cache Architecture (implemented)

```
Tier 1: R2 parquet (options raw data) ✅
Tier 2: SQLite result cache ✅ (today)
Tier 3: R2 precomputed results ✅ (options only, partial)
Modal: parallel options compute ❌ (next session)
```

Cache key: sha256(strategy + symbol + timeframe + sorted_params + limit + commission + slippage)
TTL: 24 hours
Table: backtest_cache (cache_key, result_json, strategy, symbol, created_at)

---

## Files to Drop at Start of Next Session
- api/main.py
- api/database.py
- api/bot_template_lp_master.py
- api/generate.py
- api/options_backtest.py (for Modal work)
- api/options_data.py (for Modal work)
- scripts/precompute_modal.py (if exists)
- static/index.html

---

## Key Numbers
- Students: 114
- Min profitable TP for 700.HK (100 lots): 0.35% (use 0.5%)
- Round trip fees 700.HK 100 shares: ~HKD 144
- Modal cost per options backtest: ~$0.01
- Cache hit ratio expected after 1 class: ~80% (most students run same symbols)
