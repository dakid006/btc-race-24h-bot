"""Selective EMA-band trend bot for CBBTC/USDC (single asset, long only).

Strategy: follow the slow trend but trade rarely. Track the EMA(50) of closes.
When the close rises 1.0% above the EMA it buys the full stake; when it falls
1.0% below the EMA it sells everything. Between the bands it holds whatever it
has. The wide band means it only acts on moves large enough to cover the
round-trip cost (~26 bps), avoids churning in a sideways market, and stays in
cash through a grinding downtrend instead of buying it.

Everything is integer-friendly and deterministic: EMAs are kept in floating
point (IEEE 754 on + - * / only, correctly rounded), comparisons are exact,
and the same bars always produce the same orders. No network, no wall clock,
no randomness, no file access beyond the protocol. Uses the vendored
makefun_bars shim if present, else speaks the protocol directly.
"""

from __future__ import annotations

import json
import sys

try:
    from vendor.makefun_bars import run as _run  # type: ignore
except Exception:
    _run = None


BPS = 10000
EMA_N = 50
ENTRY_BAND_BPS = 100  # 1.0% above EMA to buy
EXIT_BAND_BPS = 100  # 1.0% below EMA to sell
MAX_SLIPPAGE_BPS = 1000  # 10% tolerance so a refused fill is rare; impact is small at our size


class _State:
    def __init__(self):
        self.ema_num = None  # EMA(50) of closes, scaled by 1e6 as int
        self.ema_den = None
        self.started = False


def _ema_init(_s, price):
    # EMA seed: first close; alpha = 2/(50+1) exactly as rational 2/51
    return price


def _ema_update(ema, price):
    # ema = (2*price + 49*ema) / 51, exact integer, truncated toward zero
    return (2 * price + 49 * ema) // 51


def _band_trigger(price, ema):
    # price >= ema * (1 + 100/10000)  -> price*10000 >= ema*10100
    return price * 10000 >= ema * (10000 + ENTRY_BAND_BPS)


def _band_exit(price, ema):
    # price <= ema * (1 - 100/10000)
    return price * 10000 <= ema * (10000 - EXIT_BAND_BPS)


_STATE = _State()


def _on_batch(state, batch):
    # state is the shim's BarsState; batch is the raw batch dict.
    # Track EMA over closes of CBBTC (single symbol).
    symbol = "CBBTC"
    # Try to get the close from state first, then from the batch bars.
    close = None
    try:
        close = state.close(symbol)
    except Exception:
        close = None
    if close is None:
        bars = batch.get("bars", []) if isinstance(batch, dict) else []
        for b in bars:
            if isinstance(b, dict) and b.get("symbol") == symbol:
                try:
                    close = int(b.get("c"))
                except Exception:
                    close = None
                break
    if close is None:
        return []
    if _STATE.ema_num is None:
        _STATE.ema_num = _ema_init(_STATE, close)
    else:
        _STATE.ema_num = _ema_update(_STATE.ema_num, close)
    ema = _STATE.ema_num

    # Not while warming up: orders then are rejected.
    try:
        if state.warming_up:
            return []
    except Exception:
        pass

    held = False
    qty = 0
    try:
        pos = state.positions.get(symbol) if hasattr(state, "positions") else None
        if pos is not None:
            held = True
            try:
                qty = pos.qty
            except Exception:
                try:
                    qty = int(pos[0])
                except Exception:
                    qty = 0
    except Exception:
        held = False

    if not held:
        if not _band_trigger(close, ema):
            return []
        # Buy the full stake the caps allow, sized by cash.
        try:
            cash = int(state.cash)
            fees = state.hello.get("fees", {}) if isinstance(state.hello, dict) else {}
            min_notional = int(fees.get("min_notional_quote_units", 1000000))
            caps = state.hello.get("caps", {}) if isinstance(state.hello, dict) else {}
            max_weight_bps = int(caps.get("max_weight_bps", 10000))
            equity = int(state.equity)
        except Exception:
            return []
        if cash <= 0:
            return []
        # Spend all cash but keep under the weight cap room.
        try:
            room = state.weight_room(symbol)  # type: ignore[attr-defined]
        except Exception:
            room = (max_weight_bps * equity // BPS) if equity > 0 else cash
        spend = cash if cash < room else room
        if spend <= 0 or spend < min_notional:
            return []
        return [{"side": "buy", "symbol": symbol,
                 "notional_quote_units": str(spend), "max_slippage_bps": MAX_SLIPPAGE_BPS}]
    else:
        if not _band_exit(close, ema):
            return []
        if qty <= 0:
            return []
        return [{"side": "sell", "symbol": symbol,
                 "qty": str(qty), "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def _fallback_run():
    # Minimal protocol speaker if the vendored shim is absent.
    inp = sys.stdin.buffer
    out = sys.stdout
    hello = None
    cash = 0
    equity = 0
    positions = {}
    ema = None
    warmup_bars = 0
    for raw in inp:
        try:
            line = raw.decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        kind = msg.get("type")
        if kind == "hello":
            hello = msg
            cap = msg.get("capital", {})
            try:
                cash = int(cap.get("quote_units", 1000000000))
            except Exception:
                cash = 1000000000
            equity = cash
            warmup_bars = int(msg.get("warmup_bars", 0) or 0)
        elif kind == "batch":
            seq = int(msg.get("seq", 0))
            acct = msg.get("account", {}) or {}
            try:
                cash = int(acct.get("cash_quote_units", cash))
            except Exception:
                pass
            try:
                equity = int(acct.get("equity_quote_units", equity))
            except Exception:
                pass
            positions = {}
            for p in acct.get("positions", []) or []:
                try:
                    positions[p.get("symbol")] = int(p.get("qty", 0))
                except Exception:
                    pass
            close = None
            for b in msg.get("bars", []) or []:
                if b.get("symbol") == "CBBTC":
                    try:
                        close = int(b.get("c"))
                    except Exception:
                        close = None
            orders = []
            if close is not None and seq >= warmup_bars:
                if ema is None:
                    ema = _ema_init(_STATE, close)
                else:
                    ema = _ema_update(ema, close)
                _STATE.ema_num = ema
                if "CBBTC" not in positions:
                    if _band_trigger(close, ema) and cash > 0:
                        sym = None
                        for s in (hello or {}).get("symbols", []) or []:
                            sym = s.get("symbol")
                            break
                        if sym is None:
                            sym = "CBBTC"
                        min_notional = 1000000
                        try:
                            min_notional = int((hello or {}).get("fees", {}).get("min_notional_quote_units", 1000000))
                        except Exception:
                            pass
                        if cash >= min_notional:
                            orders.append({"side": "buy", "client_id": "o1", "symbol": sym,
                                            "notional_quote_units": str(cash), "max_slippage_bps": MAX_SLIPPAGE_BPS})
                else:
                    if _band_exit(close, ema) and positions.get("CBBTC", 0) > 0:
                        orders.append({"side": "sell", "client_id": "o1", "symbol": "CBBTC",
                                        "qty": str(positions["CBBTC"]), "max_slippage_bps": MAX_SLIPPAGE_BPS})
            out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders}, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return


if _run is not None:
    _run(_on_batch)
else:
    _fallback_run()
