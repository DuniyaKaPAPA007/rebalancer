"""Demo / paper broker. Credentials ke bina UI try karne ke liye.

Ye asli Dhan client ka same interface deta hai par sab data nakli hai.
Koi order kabhi market mein nahi jaata.
"""
from __future__ import annotations

import random
from rebalancer.models import Position, CircuitInfo

_DEMO_HOLDINGS = [
    ("HFCL",    "21951", 4100, 210.00, 219.50),
    ("STLTECH", "18564", 1450, 620.00, 642.45),
    ("DIACABS", "12043", 2500, 372.00, 359.90),
    ("SWANDEF", "19112",  900, 980.00, 1015.00),
    ("PGEL",    "14299",  340, 2610.00, 2680.00),
    ("KAYNES",  "16305",  150, 5900.00, 6120.00),
]


class PaperClient:
    """Nakli broker -- sirf demo ke liye."""

    is_paper = True

    DEFAULT_CAPITAL = 1_00_00_000.0        # demo ka default: Rs 1 crore

    def __init__(self, capital: float | None = None, *a, **kw):
        import threading
        self._lock = threading.RLock()
        try:
            cap = float(capital if capital is not None else self.DEFAULT_CAPITAL)
            if cap != cap or cap <= 0 or cap != abs(cap):
                cap = self.DEFAULT_CAPITAL
            if cap < 10000:
                cap = 10000
        except (TypeError, ValueError, OverflowError):
            cap = self.DEFAULT_CAPITAL
        self.capital = cap
        self._ltp = {s: l for s, _, _, _, l in _DEMO_HOLDINGS}
        self._orders: dict[str, dict] = {}
        self._seq = 0
        # demo holdings ko capital ke hisaab se scale karo, taaki jo number
        # tum daalo wahi dikhe -- fixed fake portfolio nahi
        base = sum(q * l for _, _, q, _, l in _DEMO_HOLDINGS)
        k = (cap * 0.985) / base if base else 1.0
        self._cash = cap * 0.015
        # {symbol: [security_id, qty, avg]} -- fills se update hota hai
        self._pos = {s: [sid, max(1, int(q * k)), avg]
                     for s, sid, q, avg, _ in _DEMO_HOLDINGS}
        self._sym_of = {sid: s for s, sid, _, _, _ in _DEMO_HOLDINGS}

    # ---- read side ----------------------------------------------------
    def available_cash(self) -> float:
        return round(self._cash, 2)

    def funds(self) -> dict:
        return {"availabelBalance": self.available_cash()}

    def holdings(self) -> list[Position]:
        with self._lock:
            return [Position(symbol=s, security_id=v[0], total_qty=v[1],
                             available_qty=v[1], avg_price=v[2])
                    for s, v in list(self._pos.items()) if v[1] > 0]

    def ltp(self, security_ids, segment: str = "NSE_EQ") -> dict[str, float]:
        out = {}
        for sid in security_ids:
            sym = self._sym_of.get(sid, sid)
            out[sid] = self._ltp.get(sym) or self._ltp.get(sid) or 0.0
        return out

    def quotes(self, security_ids, symbol_of, segment: str = "NSE_EQ"):
        out = {}
        for sid in security_ids:
            sym = symbol_of.get(sid, sid)
            px = self._ltp.get(sym) or self._ltp.get(sid) or 0.0
            if not px:
                continue
            self._sym_of.setdefault(sid, sym)
            out[sym] = CircuitInfo(symbol=sym, ltp=px, upper=px * 1.20,
                                   lower=px * 0.80, prev_close=px,
                                   volume=int(9_00_00_000 / max(px, 1)))
        return out

    def set_prices(self, mapping: dict[str, float]) -> None:
        """Watchlist se LTP le lo taaki demo plan realistic bane."""
        self._ltp.update(mapping)

    def seed_from(self, stocks: list[dict], capital: float | None = None) -> None:
        """Demo portfolio ko backtest ke ek period se bana do.

        Har naam ko barabar hissa, entry price us period ka start price.
        Isse demo mein asli rebalance dikhta hai -- sab kuch naya nahi.
        """
        capital = float(capital or self.capital)
        self.capital = capital
        usable = [s for s in stocks if s.get("start")]
        if not usable:
            return
        slice_v = capital * 0.99 / len(usable)
        self._pos, self._sym_of, self._ltp = {}, {}, dict(self._ltp)
        for i, st in enumerate(usable, 900):
            sym, px = st["nse"], float(st["start"])
            qty = int(slice_v // px)
            if qty <= 0:
                continue
            sid = f"SEC{i}"
            self._pos[sym] = [sid, qty, px]
            self._sym_of[sid] = sym
            self._ltp[sym] = float(st.get("ltp") or px)   # period ka end price
        self._cash = capital * 0.01

    def register(self, security_ids: dict[str, str]) -> None:
        """{symbol: security_id} -- taaki fill par sahi scrip update ho."""
        with self._lock:
            self._sym_of.update({sid: sym for sym, sid in security_ids.items()})

    # ---- write side (kuch nahi karta) ---------------------------------
    def find_order_by_correlation(self, correlation_id: str):
        return None

    def place_order(self, *, security_id, side, qty, **kw) -> dict:
        """Turant poora fill maan lete hain, aur cash/holdings update karte hain."""
        with self._lock:
            self._seq += 1
            oid = f"PAPER{self._seq:06d}"
            sym = self._sym_of.get(security_id) or kw.get("symbol") or security_id
            try:
                px = float(kw.get("price") or self._ltp.get(sym) or 0.0)
            except (TypeError, ValueError):
                px = 0.0
            if side.upper().startswith("S"):
                self._cash += qty * px
                if sym in self._pos:
                    self._pos[sym][1] = max(0, self._pos[sym][1] - qty)
            else:
                self._cash -= qty * px
                # prevent negative cash? allow but warn
                if self._cash < -0.01:
                    self._cash = max(self._cash, -1000)  # cap negative drift
                if sym in self._pos:
                    cur_q, cur_a = self._pos[sym][1], self._pos[sym][2]
                    new_q = cur_q + qty
                    self._pos[sym][2] = ((cur_q * cur_a + qty * px) / new_q) if new_q else px
                    self._pos[sym][1] = new_q
                else:
                    self._pos[sym] = [security_id, qty, px]
            self._orders[oid] = {"orderId": oid, "orderStatus": "TRADED",
                                 "filledQty": qty, "averageTradedPrice": px}
            return self._orders[oid]

    def order(self, order_id: str) -> dict:
        return self._orders.get(order_id, {"orderStatus": "TRADED"})

    def all_orders(self) -> list[dict]:
        return list(self._orders.values())

    def modify_to_market(self, order_id, qty) -> dict:
        return self._orders.get(order_id, {})

    def cancel(self, order_id) -> dict:
        return {"orderId": order_id, "orderStatus": "CANCELLED"}
