"""QuantX -- SPXW 0DTE Pre-compute Engine.

Reads local parquet files from E:\\Data\\Options\\SPXW\\greeks_1min_daily\\
(no R2 download needed), runs backtests in parallel, saves results to R2 bucket
"quantx-results" (or local fallback if R2 is read-only), and prints alerts for
high-Sharpe discoveries.

Usage:
    python scripts/precompute_spxw.py --dry-run
    python scripts/precompute_spxw.py --workers 8
    python scripts/precompute_spxw.py --symbol SPXW --sharpe-threshold 1.5
"""

from __future__ import annotations

import sys
import os
import json
import time
import argparse
import logging
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Bring project root onto the path so `from api.options_backtest import ...` works.
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Point the options_data cache dir at the local parquet root BEFORE importing.
# Our monkey-patches below replace the R2 download path entirely -- this env
# var just prevents the engine from creating an empty options_cache/ dir.
LOCAL_DATA_ROOT = Path(r"E:\Data\Options")
os.environ.setdefault("OPTIONS_CACHE_DIR", str(LOCAL_DATA_ROOT))

import boto3  # noqa: E402
import pandas as pd  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("precompute_spxw.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("precompute")

# ── R2 config (results bucket) ────────────────────────────────────────────────
R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    "https://7f835882a6c11ee760fe4e96eb8cbef2.r2.cloudflarestorage.com",
)
# These creds have read+write access to the quantx-results bucket (where we
# save backtest results). Reads from options-data still use the credentials
# embedded in api/options_data.py, which are scoped for read-only access.
R2_ACCESS_KEY = os.environ.get("R2_RESULTS_ACCESS_KEY") or os.environ.get("R2_ACCESS_KEY") or ""
R2_SECRET_KEY = os.environ.get("R2_RESULTS_SECRET_KEY") or os.environ.get("R2_SECRET_KEY") or ""
R2_RESULTS_BUCKET = "quantx-results"
LOCAL_RESULTS_DIR = PROJECT_ROOT / "precompute_results"


def get_r2():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


# ══════════════════════════════════════════════════════════════════════════════
# LOCAL DATA OVERRIDE -- monkey-patch api.options_data to read from E: drive
# ══════════════════════════════════════════════════════════════════════════════
_KEEP_COLS = [
    "symbol", "expiration", "strike", "right", "timestamp",
    "bid", "ask",
    "delta", "theta", "gamma", "vega",
    "implied_vol", "underlying_price",
]


def _local_parquet_path(symbol: str, date_str: str) -> Path:
    return LOCAL_DATA_ROOT / symbol.upper() / "greeks_1min_daily" / f"{date_str}.parquet"


def _load_and_clean(symbol: str, date_str: str) -> pd.DataFrame:
    """Read + quality-filter + project to 13 cols. Raises FileNotFoundError if missing."""
    path = _local_parquet_path(symbol, date_str)
    if not path.exists():
        raise FileNotFoundError(str(path))
    # Read all needed columns in one pass (faster than double-read schema-check)
    df = pd.read_parquet(path, columns=_KEEP_COLS + ["iv_error"])
    df = df[(df["iv_error"] != -1) & (df["underlying_price"] > 0)]
    return df[_KEEP_COLS].copy()


def install_local_patches() -> None:
    """Replace options_data's R2 hooks with local-file readers."""
    import api.options_data as _od
    import threading
    from collections import OrderedDict

    # In-memory LRU of {(symbol, date) -> {minute_of_day: DataFrame}}
    _mem_lock = threading.Lock()
    _mem_cache: OrderedDict = OrderedDict()
    _MAX = 8  # higher than stock engine since we have RAM to spare

    def local_load_day(symbol: str, date_str: str) -> dict:
        key = (symbol.upper(), date_str)
        with _mem_lock:
            if key in _mem_cache:
                _mem_cache.move_to_end(key)
                return _mem_cache[key]
        df = _load_and_clean(symbol, date_str)
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        ns = df["timestamp"].values.astype("int64")
        mod = (ns // 60_000_000_000) % 1440
        by_mod = {int(k): g.reset_index(drop=True) for k, g in df.groupby(mod, sort=False)}
        with _mem_lock:
            _mem_cache[key] = by_mod
            _mem_cache.move_to_end(key)
            while len(_mem_cache) > _MAX:
                _mem_cache.popitem(last=False)
        return by_mod

    def local_get_chain(symbol: str, date_str: str, entry_time: str = "09:45") -> pd.DataFrame:
        by_mod = local_load_day(symbol, date_str)
        h, m = entry_time.split(":")
        return by_mod.get(int(h) * 60 + int(m), pd.DataFrame())

    def local_get_available_dates(symbol: str) -> list:
        sym_dir = LOCAL_DATA_ROOT / symbol.upper() / "greeks_1min_daily"
        if not sym_dir.exists():
            return []
        out = []
        for p in sym_dir.glob("*.parquet"):
            name = p.stem
            if len(name) == 10 and name[4] == "-":
                out.append(name)
        return sorted(set(out))

    def local_ensure_day_cached(symbol: str, date_str: str):
        # Prewarm is a no-op -- we read directly from E: on demand.
        p = _local_parquet_path(symbol, date_str)
        if not p.exists():
            raise FileNotFoundError(str(p))
        return p

    _od._load_day = local_load_day
    _od.get_chain_for_date = local_get_chain
    _od.get_available_dates = local_get_available_dates
    _od._ensure_day_cached = local_ensure_day_cached
    # Invalidate any stale TTL cache inside options_data
    if hasattr(_od, "invalidate_dates_cache"):
        _od.invalidate_dates_cache()
    log.info("Local data patches installed -> %s", LOCAL_DATA_ROOT)


# ══════════════════════════════════════════════════════════════════════════════
# PARAMETER GRID -- Phase 1: SPXW 0DTE default params only
# ══════════════════════════════════════════════════════════════════════════════
SYMBOLS = ["SPXW"]
STRATEGIES = [
    "SHORT_PUT_SPREAD",
    "SHORT_CALL_SPREAD",
    "IRON_CONDOR",
    "SHORT_STRANGLE",
    "LONG_CALL",
    "LONG_PUT",
]
DTES = [0]
PERIODS = {"1Y": 1, "2Y": 2, "3Y": 3}

DEFAULT_PARAMS = {
    "short_strike_value": -0.30,
    "short_call_strike_value": 0.30,
    "wing_width_value": 5.0,
    "call_wing_width_value": 5.0,
    "profit_target_pct": 50,
    "stop_loss_pct": 200,
}

ENTRY_PARAMS = {
    "entry_time": "09:45",
    "entry_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
    "entry_frequency": "DAILY",
    "dte_tolerance": 0,
    "contracts": 1,
    "commission_per_contract": 0.65,
    "slippage_pct": 0.0,
    "starting_capital": 10000,
    "check_exit_times": [
        "09:45", "10:00", "10:30", "11:00", "11:30",
        "12:00", "12:30", "13:00", "13:30", "14:00",
        "14:30", "15:00", "15:30", "15:45",
    ],
    "exit_time": "15:45",
}


def get_date_range(period_years: int) -> tuple:
    """End = latest available local SPXW date; start = N years back."""
    from api.options_data import get_available_dates
    dates = get_available_dates("SPXW")
    end_str = dates[-1] if dates else "2026-04-20"
    end_d = datetime.strptime(end_str, "%Y-%m-%d").date()
    start_d = date(end_d.year - period_years, end_d.month, end_d.day)
    return start_d.strftime("%Y-%m-%d"), end_str


def build_config(symbol: str, strategy: str, dte: int, period_years: int) -> dict:
    start, end = get_date_range(period_years)
    return {
        "symbol": symbol,
        "strategy_type": strategy,
        "target_dte": dte,
        "short_strike_method": "DELTA",
        "wing_width_method": "POINTS_OTM",
        "short_call_strike_method": "DELTA",
        "call_wing_width_method": "POINTS_OTM",
        "start_date": start,
        "end_date": end,
        **DEFAULT_PARAMS,
        **ENTRY_PARAMS,
    }


# ══════════════════════════════════════════════════════════════════════════════
# RESULT STORAGE (R2 + local fallback)
# ══════════════════════════════════════════════════════════════════════════════
def _result_key(symbol, strategy, dte, period, params_hash="default") -> str:
    return f"results/{symbol}/{strategy}/{dte}DTE/{period}/{params_hash}.json"


def _index_key() -> str:
    return "results/index.json"


class ResultStore:
    """Writes to R2 when possible, falls back to a local directory when not."""

    def __init__(self, s3, r2_ok: bool):
        self.s3 = s3
        self.r2_ok = r2_ok
        LOCAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Index gets updated in-memory, flushed after each save to avoid
        # a race between parallel workers overwriting each other.
        import threading
        self._lock = threading.Lock()
        self._index = self._load_index()

    def _local_path(self, key: str) -> Path:
        return LOCAL_RESULTS_DIR / key

    def _load_index(self) -> dict:
        if self.r2_ok:
            try:
                obj = self.s3.get_object(Bucket=R2_RESULTS_BUCKET, Key=_index_key())
                return json.loads(obj["Body"].read().decode())
            except Exception:
                pass
        local = self._local_path(_index_key())
        if local.exists():
            return json.loads(local.read_text())
        return {"results": [], "last_updated": None}

    def _write_blob(self, key: str, body: bytes) -> str:
        """Write to R2 if possible, else local. Returns 'r2' or 'local'."""
        if self.r2_ok:
            try:
                self.s3.put_object(
                    Bucket=R2_RESULTS_BUCKET, Key=key, Body=body,
                    ContentType="application/json",
                )
                return "r2"
            except Exception as e:
                log.warning("R2 write failed, falling back to local: %s", str(e)[:120])
                self.r2_ok = False
        local = self._local_path(key)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(body)
        return "local"

    def already_computed(self, symbol, strategy, dte, period, params_hash="default") -> bool:
        key = _result_key(symbol, strategy, dte, period, params_hash)
        if self.r2_ok:
            try:
                self.s3.head_object(Bucket=R2_RESULTS_BUCKET, Key=key)
                return True
            except Exception:
                pass
        return self._local_path(key).exists()

    def save(self, symbol, strategy, dte, period, result, params_hash="default") -> str:
        payload = {
            "symbol": symbol,
            "strategy": strategy,
            "dte": dte,
            "period": period,
            "params_hash": params_hash,
            "computed_at": datetime.utcnow().isoformat(),
            "metrics": result["metrics"],
            "trade_log": result["trade_log"],
        }
        key = _result_key(symbol, strategy, dte, period, params_hash)
        where = self._write_blob(key, json.dumps(payload, default=str).encode())

        with self._lock:
            # Upsert the index entry
            self._index["results"] = [
                r for r in self._index["results"]
                if not (r["symbol"] == symbol and r["strategy"] == strategy
                        and r["dte"] == dte and r["period"] == period
                        and r.get("params_hash") == params_hash)
            ]
            m = result["metrics"]
            self._index["results"].append({
                "symbol": symbol, "strategy": strategy, "dte": dte,
                "period": period, "params_hash": params_hash,
                "total_pnl": m.get("total_pnl"),
                "total_trades": m.get("total_trades"),
                "win_rate": m.get("win_rate"),
                "sharpe_ratio": m.get("sharpe_ratio"),
                "sortino_ratio": m.get("sortino_ratio"),
                "max_drawdown": m.get("max_drawdown"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
                "profit_factor": m.get("profit_factor"),
                "avg_pnl_per_trade": m.get("avg_pnl_per_trade"),
                "total_premium_collected": m.get("total_premium_collected"),
                "pct_expired_worthless": m.get("pct_expired_worthless"),
                "pct_closed_profit_target": m.get("pct_closed_profit_target"),
                "avg_dte_at_entry": m.get("avg_dte_at_entry"),
                "computed_at": datetime.utcnow().isoformat(),
            })
            self._index["last_updated"] = datetime.utcnow().isoformat()
            self._write_blob(_index_key(), json.dumps(self._index, default=str).encode())
        return where


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════
SHARPE_ALERT_THRESHOLD = 1.5
HIGH_SHARPE_RESULTS: list = []


def check_alert(symbol, strategy, dte, period, metrics):
    sharpe = metrics.get("sharpe_ratio") or 0
    total_trades = metrics.get("total_trades") or 0
    if sharpe >= SHARPE_ALERT_THRESHOLD and total_trades >= 10:
        entry = {
            "symbol": symbol, "strategy": strategy, "dte": dte, "period": period,
            "sharpe": sharpe,
            "win_rate": metrics.get("win_rate") or 0,
            "total_pnl": metrics.get("total_pnl") or 0,
            "max_dd_pct": metrics.get("max_drawdown_pct") or 0,
            "trades": total_trades,
        }
        HIGH_SHARPE_RESULTS.append(entry)
        log.info(
            "HIGH SHARPE: %s %s %dDTE %s | Sharpe=%.2f WR=%.1f%% "
            "PnL=$%.0f Trades=%d",
            symbol, strategy, dte, period,
            sharpe, entry["win_rate"], entry["total_pnl"], total_trades,
        )


# ══════════════════════════════════════════════════════════════════════════════
# WORKER
# ══════════════════════════════════════════════════════════════════════════════
def run_one(task: dict) -> dict:
    from api.options_backtest import run_options_backtest
    t0 = time.time()
    try:
        config = build_config(
            task["symbol"], task["strategy"], task["dte"], task["period_years"],
        )
        result = run_options_backtest(config)
        elapsed = time.time() - t0
        m = result["metrics"]
        log.info(
            "OK  %s %s %dDTE %s | %d trades | Sharpe=%.2f | WR=%.1f%% | "
            "PnL=$%.0f | %.1fs",
            task["symbol"], task["strategy"], task["dte"], task["period"],
            m.get("total_trades") or 0, m.get("sharpe_ratio") or 0,
            m.get("win_rate") or 0, m.get("total_pnl") or 0, elapsed,
        )
        return {"status": "ok", "task": task, "result": result, "elapsed": elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        log.error("ERR %s %s %dDTE %s: %s",
                  task["symbol"], task["strategy"], task["dte"], task["period"], e)
        return {"status": "error", "task": task, "error": str(e), "elapsed": elapsed}


# ══════════════════════════════════════════════════════════════════════════════
# VERIFY + MAIN
# ══════════════════════════════════════════════════════════════════════════════
def verify_local_data() -> bool:
    test_date = "2024-01-02"
    path = _local_parquet_path("SPXW", test_date)
    if not path.exists():
        log.error("Local data not found: %s", path)
        log.error("Check %s", LOCAL_DATA_ROOT / "SPXW" / "greeks_1min_daily")
        return False
    df = pd.read_parquet(path)
    log.info("Local data check: %s", test_date)
    log.info("  Shape:   %s", df.shape)
    log.info("  Columns (%d): %s", len(df.columns), list(df.columns))
    log.info("  File size: %.1f MB", path.stat().st_size / 1024 / 1024)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        times = sorted(ts.dt.strftime("%H:%M").unique())
        log.info("  Minute count: %d   First 5: %s   Last 5: %s",
                 len(times), times[:5], times[-5:])
    return True


def verify_r2_writes(s3) -> bool:
    try:
        s3.head_bucket(Bucket=R2_RESULTS_BUCKET)
    except Exception as e:
        log.warning("R2 bucket '%s' not accessible: %s",
                    R2_RESULTS_BUCKET, str(e)[:100])
        return False
    try:
        probe = f"_probe/{int(time.time())}.txt"
        s3.put_object(Bucket=R2_RESULTS_BUCKET, Key=probe, Body=b"probe")
        s3.delete_object(Bucket=R2_RESULTS_BUCKET, Key=probe)
        return True
    except Exception as e:
        log.warning("R2 write probe failed: %s", str(e)[:100])
        return False


def build_task_list(store: ResultStore, skip_existing: bool) -> list:
    tasks = []
    for symbol in SYMBOLS:
        for strategy in STRATEGIES:
            for dte in DTES:
                for period, years in PERIODS.items():
                    if skip_existing and store.already_computed(
                        symbol, strategy, dte, period
                    ):
                        log.info("SKIP (exists): %s %s %dDTE %s",
                                 symbol, strategy, dte, period)
                        continue
                    tasks.append({
                        "symbol": symbol, "strategy": strategy,
                        "dte": dte, "period": period, "period_years": years,
                    })
    return tasks


def main():
    parser = argparse.ArgumentParser(description="SPXW 0DTE pre-compute engine")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-skip", action="store_true",
                        help="Recompute even if results already exist")
    parser.add_argument("--sharpe-threshold", type=float, default=1.5)
    args = parser.parse_args()

    global SHARPE_ALERT_THRESHOLD
    SHARPE_ALERT_THRESHOLD = args.sharpe_threshold

    log.info("=" * 60)
    log.info("SPXW 0DTE pre-compute engine")
    log.info("  Symbols:    %s", SYMBOLS)
    log.info("  Strategies: %s", STRATEGIES)
    log.info("  DTEs:       %s", DTES)
    log.info("  Periods:    %s", list(PERIODS.keys()))
    log.info("  Workers:    %d", args.workers)
    log.info("  Sharpe alert: >= %.2f", SHARPE_ALERT_THRESHOLD)
    log.info("=" * 60)

    if not verify_local_data():
        sys.exit(1)

    install_local_patches()

    s3 = get_r2()
    r2_ok = verify_r2_writes(s3)
    log.info("R2 results bucket: %s",
             "writable" if r2_ok else "read-only / inaccessible (local fallback)")
    log.info("Local results dir: %s", LOCAL_RESULTS_DIR)

    store = ResultStore(s3, r2_ok=r2_ok)
    tasks = build_task_list(store, skip_existing=not args.no_skip)
    total = len(tasks)
    log.info("Tasks queued: %d", total)

    if args.dry_run:
        for t in tasks:
            print(f"  {t['symbol']} {t['strategy']} {t['dte']}DTE {t['period']}")
        print(f"\nTotal: {total} tasks")
        return

    if total == 0:
        log.info("Nothing to compute -- all results up to date.")
        return

    t_start = time.time()
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=args.workers,
                            thread_name_prefix="precompute") as pool:
        futures = {pool.submit(run_one, task): task for task in tasks}
        for future in as_completed(futures):
            done += 1
            r = future.result()
            if r["status"] == "ok":
                task = r["task"]
                try:
                    where = store.save(task["symbol"], task["strategy"],
                                       task["dte"], task["period"], r["result"])
                    check_alert(task["symbol"], task["strategy"],
                                task["dte"], task["period"], r["result"]["metrics"])
                    log.debug("Saved to %s", where)
                except Exception as e:
                    log.error("Save failed: %s", e)
                    errors += 1
            else:
                errors += 1
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            log.info("Progress: %d/%d   errors=%d   rate=%.2f/s   eta=%.1fmin",
                     done, total, errors, rate, eta / 60)

    total_elapsed = time.time() - t_start
    log.info("=" * 60)
    log.info("COMPLETE: %d tasks in %.1f minutes  (errors=%d)",
             done, total_elapsed / 60, errors)

    if HIGH_SHARPE_RESULTS:
        print("\n" + "=" * 78)
        print(f"HIGH SHARPE STRATEGIES (Sharpe >= {SHARPE_ALERT_THRESHOLD})")
        print("=" * 78)
        print(f"{'Symbol':<7} {'Strategy':<20} {'DTE':<4} {'Per':<4} "
              f"{'Sharpe':>7} {'WR%':>6} {'P&L':>10} {'MaxDD%':>7} {'Trades':>7}")
        print("-" * 78)
        for r in sorted(HIGH_SHARPE_RESULTS, key=lambda x: x["sharpe"], reverse=True):
            print(f"{r['symbol']:<7} {r['strategy']:<20} {r['dte']:<4} "
                  f"{r['period']:<4} {r['sharpe']:>7.2f} "
                  f"{r['win_rate']:>5.1f}% ${r['total_pnl']:>8,.0f} "
                  f"{r['max_dd_pct']:>6.1f}% {r['trades']:>7}")
        print("=" * 78)
        print(f"Total high-Sharpe discoveries: {len(HIGH_SHARPE_RESULTS)}")
    else:
        log.info("No results above Sharpe threshold of %.2f", SHARPE_ALERT_THRESHOLD)

    log.info("=" * 60)


if __name__ == "__main__":
    main()
