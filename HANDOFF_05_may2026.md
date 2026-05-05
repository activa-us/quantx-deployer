# QuantX Deployer — Handoff 05 May 2026

## Session Summary
Full day session. Major milestone: **first confirmed live trades via LongPort**.

---

## Git Commits This Session (chronological)

| Commit | Change |
|--------|--------|
| `46cfcc4` | Remove all IBKR code |
| `d5baab3` | fix: generate.py is_grid fix (SYMMETRIC_GRID skips signal code) |
| `e300c68` | fix: deploy complete bot_template_lp_master.py with GridState + DRY_RUN |
| `87e2c4d` | fix: remove silent fallback in deploy, add dry_run to DeployReq |
| `3ffe696` | feat: /api/admin/nuke-scripts endpoint |
| `a1ee0ce` | feat: /api/admin/clear-bot-scripts endpoint |
| `82b683d` | fix: remove __SIGNAL_FUNCTIONS_BLOCK__ comment placeholder |
| `fc6f9e1` | fix: trades dashboard + DRY badge |
| `d50da9f` | fix: LongBridge fees in grid PnL + deduplicate on_fill |
| Backend (CC) | feat: is_dry_run in strategies + clone endpoint |
| `latest` | feat: bot mode (dry/live) at creation + clone + live P/L in list |
| `latest` | fix: OrderSide import missing → live orders were failing |
| `latest` | fix: clone mode choice (OK=flip, Cancel=same) |
| `latest` | feat: Lots field in New Bot form with auto-fill |
| `f32b181` | fix: duplicate processes + LIMIT 500 + log dedup |

---

## Architecture: Current State

### Bot Lifecycle
```
New Bot form (mode=dry/live, symbol, strategy, lots, params)
  → createBot() → POST /api/strategy → save_strategy() → DB
  → Deploy button → POST /api/deploy → generate_lp_master_bot()
  → script at /data/bots/{email_safe}_lp_master.py
  → _launch_bot() → subprocess.Popen → Railway process
  → bot writes to /data/logs/{email_safe}_lp_master.log
  → bot POSTs trades to /api/trade → trades table in SQLite
```

### Bot Template: bot_template_lp_master.py (958 lines)
Key features:
- `DRY_RUN = __DRY_RUN__` — substituted at generation time
- `GridState` class — event-driven grid bot (quote WebSocket)
- `StrategyState` class — signal-based bot (60s bar poll)
- `calc_hk_fees(price, qty, side)` — LongBridge fee calculation
  - Buy: brokerage 0.03% + platform HKD15 + stamp 0.1% + SFC 0.0027% + HKEX 0.005%
  - Sell: same minus stamp duty (HK abolished sell-side Oct 2023)
- `_processed_oids` set on GridState — prevents duplicate on_fill
- `logger.propagate = False` — prevents double log lines

### Fee Reality Check (700.HK, 100 shares ~HK$467)
- Buy fees: ~HKD 80
- Sell fees: ~HKD 64
- Round trip: ~HKD 144
- At 0.3% TP: HKD 140 gross — UNPROFITABLE after fees
- At 0.5% TP: HKD 233 gross → HKD 89 net ✅
- **Minimum profitable TP: ~0.35%**

### generate.py
- `is_grid` check skips `generate_signal_code()` for SYMMETRIC_GRID
- Lots flows: `risk.lots` → `strategies_meta[i].risk` → `__STRATEGIES_LIST__`
- Template reads: `risk.get('lots', 100)`

### Database (strategies table)
Fields: email, strategy_id, strategy_name, symbol, arena, timeframe,
conditions_json, exit_rules_json, risk_json, is_active, mode, library_id,
custom_script, broker, **is_dry_run** (added this session via ALTER TABLE)

### Trades table
Fields: id, email, strategy_id, symbol, side, price, qty, pnl,
cumulative_pnl, timestamp
- `get_trades()` now LIMIT 500 (was returning 14k+ rows)
- Side values: 'bot'/'sld' (grid), 'buy'/'sell' (signal)
- Grid dry run: sides are 'BOT'/'SLD' 

---

## Confirmed Working ✅
- Live order placement (MSFT confirmed, BABA insufficient funds)
- Dry run grid bot (700.HK cycles working, fees calculated)
- Mode badges (🧪 Dry / 🔴 Live) in bot list and detail
- Filter by mode (All / Live / Dry Run / Off)
- Clone bot with mode choice (OK=flip mode, Cancel=same mode)
- Lots field in New Bot form with auto-fill from symbol API
- Trade history showing P/L in dashboard
- Single deploy button per mode (purple=dry, green=live)
- nuke-scripts endpoint for emergency cleanup

---

## Known Outstanding Issues

### Critical (fix before class)
1. **Duplicate log lines** — partially fixed with f32b181, monitor after redeploy
2. **Duplicate trade records** — still seeing 2x because _simulate_fill threads
   fire on_fill twice per event in rapid quote tick scenarios. _processed_oids
   helps but doesn't fully eliminate in dry run. Accept for now in dry run;
   live trading should be single-fire.
3. **Top 5 dashboard metrics empty** — Return/Sharpe/Win Rate/Max DD/Trades
   come from backtest_results, not live trades. Need to compute from trades DB.

### Important (before class)
4. **Live equity curve** — no chart on bot detail dashboard while running
5. **Shared backtest cache** — Phase 1: hash(strategy+symbol+params) → SQLite
   First student pays compute, everyone else gets instant (~50ms)
6. **Signal bot live test** — only grid bot confirmed live. Signal bots
   (EMA cross, Turtle etc.) use bar poll loop, untested live.
7. **quantx-central** — instructor dashboard still unreachable (404/CORS)

### Nice to Have
8. Modal.com parallel optimization (Phase 2) — already has precompute_modal.py
9. Export Strategy button for advanced students (IBKR)
10. Pause mode — stop process but leave orders open (vs Stop = cancel all)

---

## Admin Endpoints
```
POST /api/admin/nuke-scripts  {"pin":"quantx2025","email":"..."}
  → kills process, deletes bot .py files, marks DB stopped

POST /api/admin/clear-bot-scripts  {"pin":"quantx2025","email":"..."}
  → same as nuke-scripts (legacy)

GET  /api/debug/bot-script?email=...&key=quantx2025
  → returns generated script content (for debugging)

GET  /api/debug/fs-inspect?email=...&key=quantx2025
  → returns all bot files on volume with line counts and line 419
```

---

## Files To Keep Track Of (drop in chat when working on them)
- `api/bot_template_lp_master.py` — for bot logic changes
- `api/generate.py` — for code generation changes  
- `api/main.py` — for route/deploy changes
- `api/database.py` — for schema/query changes
- `static/index.html` — for UI changes (7900+ lines, drop full file)

## Workflow Reminder
- Claude Code for mechanical multi-file changes
- Drop files in chat for logic-sensitive single-file rewrites
- Always nuke-scripts before testing a new deploy
- Test on dry run first, then live
- Check LongPort app within 10s of live deploy to confirm orders appear
