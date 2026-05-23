#!/usr/bin/env python3
"""
Strategy name : 3004BTCUSD15MMO
Source        : MQLGen BTCUSD M15 1097449988.mq5
Symbol        : BTCUSD
CLIENT_ID     : 3004
Generated     : 2026-05-22

Logic:
  Entry runs once per completed 15-minute bar.
  Long  when the current bar opens above SMA(21) after the prior bar opened below it.
  Short when the current bar opens below SMA(21) after the prior bar opened above it.
  Exit long  after five consecutive bullish candles with body >= 7 price units.
  Exit short after five consecutive bearish candles with body >= 7 price units.

Production standards checklist:
  - DRY_RUN default with 3xxx client ID and paper Gateway port 4002
  - Streamlit-compatible 11-column CSV trade log
  - orderRef set to STRATEGY_NAME on every order and CSV row
  - risk state persisted under state/risk_{STRATEGY_NAME}.json
  - position state persisted under state/positions_BTCUSD.json
  - fill verification waits for Filled status before state updates
  - commission read from ibkr_trade.fills after confirmed fill
  - timed rotating logs, reconnect backoff, clean shutdown
"""

# =============================================================================
# MODE   : DRY-RUN (3xxx)
# PORT   : 4002 paper gateway / 4001 live gateway
# ACCOUNT: DU... (paper) / U... (live)
# To promote: change CLIENT_ID prefix, PORT, DRY_RUN, ACCOUNT_ID
# =============================================================================

import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

import pandas as pd
import pytz
from ib_insync import IB, Contract, MarketOrder, util


STRATEGY_NAME = "3004BTCUSD15MMO"
SYMBOL = "BTCUSD"
SEC_TYPE = "CRYPTO"
EXCHANGE = "PAXOS"
CURRENCY = "USD"

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "4002"))
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "3004"))
ACCOUNT_ID = os.getenv("IBKR_ACCOUNT_ID", "DUXXXXXXX")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes", "y"}

BAR_SIZE = "15 mins"
DURATION = "7 D"
WHAT_TO_SHOW = os.getenv("IBKR_WHAT_TO_SHOW", "AGGTRADES")
USE_RTH = False
ORDER_TIMEOUT_SECS = 30
MAX_RECONNECT_ATTEMPTS = 5
LOOP_SLEEP_SECS = 20

# MQL inputs.
ENTRY_AMOUNT = float(os.getenv("ENTRY_AMOUNT", "0.10"))
MA_PERIOD = 21
CANDLE_BODY_UNITS = 7.0
CONSECUTIVE_CANDLES = 5
SIGMA = 0.000001

RISK_CONFIG = {
    "max_daily_loss_usd": 1000.0,
    "max_orders_per_day": 4,
    "max_open_contracts": 1.0,
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
TRADES_DIR = os.path.join(BASE_DIR, "trades")
STATE_DIR = os.path.join(BASE_DIR, "state")

for path in (LOG_DIR, TRADES_DIR, STATE_DIR):
    os.makedirs(path, exist_ok=True)

TRADES_FILE = os.path.join(TRADES_DIR, f"trades_{STRATEGY_NAME}_all.csv")
RISK_STATE_FILE = os.path.join(STATE_DIR, f"risk_{STRATEGY_NAME}.json")
POSITION_STATE_FILE = os.path.join(STATE_DIR, f"positions_{SYMBOL}.json")

CSV_FIELDS = [
    "execId",
    "datetime",
    "symbol",
    "secType",
    "exchange",
    "currency",
    "side",
    "quantity",
    "price",
    "commission",
    "orderRef",
]

ET = pytz.timezone("US/Eastern")

logger = logging.getLogger(STRATEGY_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

file_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, f"{STRATEGY_NAME}.log"),
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def ensure_csv() -> None:
    if os.path.exists(TRADES_FILE):
        return
    with open(TRADES_FILE, "w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()


def utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_key() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def load_json(path: str, default: dict) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        logger.warning("Could not load JSON state from %s", path, exc_info=True)
    return default.copy()


def save_json(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def default_risk_state() -> dict:
    return {
        "date": today_key(),
        "daily_loss_usd": 0.0,
        "orders_today": 0,
        "halted": False,
    }


def default_position_state() -> dict:
    return {
        "position": 0.0,
        "avg_price": 0.0,
        "entry_time": "",
        "last_bar_time": "",
    }


risk_state = load_json(RISK_STATE_FILE, default_risk_state())
position_state = load_json(POSITION_STATE_FILE, default_position_state())


def reset_risk_if_new_day() -> None:
    if risk_state.get("date") == today_key():
        return
    risk_state.clear()
    risk_state.update(default_risk_state())
    save_json(RISK_STATE_FILE, risk_state)
    logger.info("Risk state reset for new trading date")


def check_risk_limits(action: str, order_qty: float) -> tuple[bool, str]:
    reset_risk_if_new_day()
    if risk_state.get("halted"):
        return False, "risk state is halted"
    if risk_state.get("daily_loss_usd", 0.0) <= -abs(RISK_CONFIG["max_daily_loss_usd"]):
        risk_state["halted"] = True
        save_json(RISK_STATE_FILE, risk_state)
        return False, "max daily loss reached"
    if risk_state.get("orders_today", 0) >= RISK_CONFIG["max_orders_per_day"]:
        return False, "max orders per day reached"
    current_position = float(position_state.get("position", 0.0))
    signed_qty = order_qty if action == "BUY" else -order_qty
    projected_position = current_position + signed_qty
    if abs(projected_position) > RISK_CONFIG["max_open_contracts"]:
        return False, "max open contracts reached"
    return True, "ok"


def log_trade(exec_id: str, side: str, quantity: float, price: float, commission: float) -> None:
    ensure_csv()
    row = {
        "execId": exec_id,
        "datetime": utc_now_string(),
        "symbol": SYMBOL,
        "secType": SEC_TYPE,
        "exchange": EXCHANGE,
        "currency": CURRENCY,
        "side": side,
        "quantity": quantity,
        "price": round(price, 6),
        "commission": round(commission, 4),
        "orderRef": STRATEGY_NAME,
    }
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=CSV_FIELDS).writerow(row)
    logger.info(
        "Trade logged | %s %.4f %s @ %.6f | commission %.4f | execId %s",
        side,
        quantity,
        SYMBOL,
        price,
        commission,
        exec_id,
    )


def build_contract() -> Contract:
    contract = Contract()
    contract.symbol = "BTC" if SEC_TYPE == "CRYPTO" and SYMBOL == "BTCUSD" else SYMBOL
    contract.secType = SEC_TYPE
    contract.exchange = EXCHANGE
    contract.currency = CURRENCY
    return contract


def normalize_ib_bars(bars) -> pd.DataFrame:
    df = util.df(bars)
    if df is None or df.empty:
        raise ValueError(
            f"IBKR returned no historical bars for {SYMBOL} with whatToShow={WHAT_TO_SHOW}"
        )
    df = df.rename(columns={"date": "datetime"})
    required = ["datetime", "open", "high", "low", "close"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"IBKR bars missing required columns: {missing}")
    df = df[required].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < 80:
        raise ValueError(f"Need at least 80 bars for indicators, got {len(df)}")
    return df


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma_21"] = sma(df["close"], MA_PERIOD)
    indicator_cols = ["ma_21"]
    if df[indicator_cols].tail(3).isna().any().any():
        raise ValueError("Indicator warm-up incomplete; latest bars contain NaN values")
    return df


def get_entry_signal(df: pd.DataFrame) -> str:
    prev = df.iloc[-2]
    older = df.iloc[-3]
    curr_open = df.iloc[-1]["open"]

    can_long = curr_open > prev["ma_21"] + SIGMA and prev["open"] < older["ma_21"] - SIGMA
    can_short = curr_open < prev["ma_21"] - SIGMA and prev["open"] > older["ma_21"] + SIGMA

    if can_long and not can_short:
        return "BUY"
    if can_short and not can_long:
        return "SELL"
    return "FLAT"


def get_exit_signal(df: pd.DataFrame, current_position: float) -> bool:
    lookback = df.iloc[-(CONSECUTIVE_CANDLES + 1):-1]
    bullish = (lookback["close"] - lookback["open"]) >= CANDLE_BODY_UNITS
    bearish = (lookback["open"] - lookback["close"]) >= CANDLE_BODY_UNITS
    if current_position > 0 and bool(bullish.all()):
        return True
    if current_position < 0 and bool(bearish.all()):
        return True
    return False


def realized_pnl(action: str, qty: float, fill_price: float) -> float:
    current_position = float(position_state.get("position", 0.0))
    avg_price = float(position_state.get("avg_price", 0.0))
    if current_position > 0 and action == "SELL":
        return (fill_price - avg_price) * min(qty, abs(current_position))
    if current_position < 0 and action == "BUY":
        return (avg_price - fill_price) * min(qty, abs(current_position))
    return 0.0


def update_position_state(action: str, qty: float, fill_price: float) -> None:
    position = float(position_state.get("position", 0.0))
    signed_qty = qty if action == "BUY" else -qty
    new_position = round(position + signed_qty, 8)

    if abs(new_position) < 1e-8:
        position_state.update({"position": 0.0, "avg_price": 0.0, "entry_time": ""})
    elif position == 0 or (position > 0) != (new_position > 0):
        position_state.update(
            {
                "position": new_position,
                "avg_price": fill_price,
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
        )
    else:
        position_state["position"] = new_position

    save_json(POSITION_STATE_FILE, position_state)


def dry_run_fill(action: str, qty: float, price: float) -> None:
    side = "BOT" if action == "BUY" else "SLD"
    exec_id = f"dry_{STRATEGY_NAME}_{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
    pnl = realized_pnl(action, qty, price)
    risk_state["daily_loss_usd"] = risk_state.get("daily_loss_usd", 0.0) + pnl
    risk_state["orders_today"] = risk_state.get("orders_today", 0) + 1
    risk_state["date"] = today_key()
    save_json(RISK_STATE_FILE, risk_state)
    update_position_state(action, qty, price)
    log_trade(exec_id, side, qty, price, 0.0)


def place_order(ib: IB, contract: Contract, action: str, qty: float, reference_price: float) -> bool:
    allowed, reason = check_risk_limits(action, qty)
    logger.info("Risk check before %s %.4f %s: %s", action, qty, SYMBOL, reason)
    if not allowed:
        return False

    if DRY_RUN:
        logger.info("DRY_RUN signal | %s %.4f %s @ reference %.6f", action, qty, SYMBOL, reference_price)
        dry_run_fill(action, qty, reference_price)
        return True

    order = MarketOrder(action, qty, account=ACCOUNT_ID)
    order.orderRef = STRATEGY_NAME
    order.tif = "DAY"

    logger.info("Submitting order | %s %.4f %s @ MKT | orderRef=%s", action, qty, SYMBOL, STRATEGY_NAME)
    ibkr_trade = ib.placeOrder(contract, order)
    deadline = time.time() + ORDER_TIMEOUT_SECS
    terminal_cancel_statuses = {"Cancelled", "Inactive", "ApiCancelled", "PendingCancel"}

    while time.time() < deadline:
        ib.sleep(0.25)
        status = ibkr_trade.orderStatus.status
        if status == "Filled":
            break
        if status in terminal_cancel_statuses:
            logger.error("Order aborted with status %s", status)
            return False

    if ibkr_trade.orderStatus.status != "Filled":
        logger.error("Fill timeout after %s seconds", ORDER_TIMEOUT_SECS)
        return False

    ib.sleep(2)
    fill_price = float(ibkr_trade.orderStatus.avgFillPrice or reference_price)
    fill_qty = float(ibkr_trade.orderStatus.filled or qty)
    commission = None
    exec_id = f"{STRATEGY_NAME}_{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"

    if ibkr_trade.fills:
        exec_id = ibkr_trade.fills[0].execution.execId
        commission_values = [
            float(fill.commissionReport.commission)
            for fill in ibkr_trade.fills
            if fill.commissionReport and fill.commissionReport.commission is not None
        ]
        if commission_values:
            commission = sum(commission_values)

    if commission is None:
        logger.error("Live fill has no commission report yet; refusing to log commission as 0.0")
        return False

    pnl = realized_pnl(action, fill_qty, fill_price) - abs(commission)
    risk_state["daily_loss_usd"] = risk_state.get("daily_loss_usd", 0.0) + pnl
    risk_state["orders_today"] = risk_state.get("orders_today", 0) + 1
    risk_state["date"] = today_key()
    save_json(RISK_STATE_FILE, risk_state)

    update_position_state(action, fill_qty, fill_price)
    log_trade(exec_id, "BOT" if action == "BUY" else "SLD", fill_qty, fill_price, commission)
    return True


def connect_ib() -> IB:
    ib = IB()
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        try:
            logger.info("Connecting to IBKR Gateway %s:%s clientId=%s", HOST, PORT, CLIENT_ID)
            ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10)
            logger.info("Connected to IBKR Gateway")
            return ib
        except Exception:
            wait = min(2 ** attempt, 60)
            logger.warning("IBKR connect attempt %s failed; retrying in %ss", attempt, wait, exc_info=True)
            time.sleep(wait)
    raise ConnectionError("Could not connect to IBKR Gateway after retries")


def fetch_bars(ib: IB, contract: Contract) -> pd.DataFrame:
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=DURATION,
        barSizeSetting=BAR_SIZE,
        whatToShow=WHAT_TO_SHOW,
        useRTH=USE_RTH,
        formatDate=1,
        keepUpToDate=False,
    )
    df = normalize_ib_bars(bars)
    return add_indicators(df)


def process_completed_bar(ib: IB, contract: Contract, df: pd.DataFrame) -> None:
    completed_bar = df.iloc[-2]
    bar_time = str(completed_bar["datetime"])
    if position_state.get("last_bar_time") == bar_time:
        return

    position_state["last_bar_time"] = bar_time
    save_json(POSITION_STATE_FILE, position_state)

    current_position = float(position_state.get("position", 0.0))
    logger.info(
        (
            "Bar closed | %s open=%.6f close=%.6f "
            "SMA21=%.6f pos=%.4f"
        ),
        bar_time,
        completed_bar["open"],
        completed_bar["close"],
        completed_bar["ma_21"],
        current_position,
    )

    if current_position != 0 and get_exit_signal(df, current_position):
        action = "SELL" if current_position > 0 else "BUY"
        logger.info("Exit signal | %s %.4f %s", action, abs(current_position), SYMBOL)
        place_order(ib, contract, action, abs(current_position), float(completed_bar["close"]))
        return

    if current_position == 0:
        entry_signal = get_entry_signal(df)
        logger.info("Entry signal | %s", entry_signal)
        if entry_signal in {"BUY", "SELL"}:
            place_order(ib, contract, entry_signal, ENTRY_AMOUNT, float(completed_bar["close"]))


def main() -> None:
    ensure_csv()
    save_json(RISK_STATE_FILE, risk_state)
    save_json(POSITION_STATE_FILE, position_state)

    logger.info(
        "Starting %s | DRY_RUN=%s | ACCOUNT_ID=%s | PORT=%s | contract=%s %s %s",
        STRATEGY_NAME,
        DRY_RUN,
        ACCOUNT_ID,
        PORT,
        SYMBOL,
        SEC_TYPE,
        EXCHANGE,
    )

    ib = connect_ib()
    contract = build_contract()
    ib.qualifyContracts(contract)

    try:
        while True:
            if not ib.isConnected():
                logger.warning("IBKR disconnected; reconnecting")
                ib = connect_ib()
                ib.qualifyContracts(contract)

            try:
                df = fetch_bars(ib, contract)
                process_completed_bar(ib, contract, df)
            except Exception:
                logger.error("Main loop error", exc_info=True)

            ib.sleep(LOOP_SLEEP_SECS)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; saving state and shutting down")
    finally:
        save_json(RISK_STATE_FILE, risk_state)
        save_json(POSITION_STATE_FILE, position_state)
        if ib.isConnected():
            ib.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
