"""Core data types. Koi API call nahi, koi I/O nahi -- sirf shapes."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Reason(str, Enum):
    """Har order kyun ban raha hai -- report aur debugging dono ke liye."""
    EXIT = "EXIT"              # list se bahar -> poora bech do
    ENTRY = "ENTRY"            # naya naam -> khareedo
    TOPUP = "TOPUP"            # carry-over, underweight -> qty badhao
    TRIM = "TRIM"              # carry-over, overweight -> qty ghatao
    OVERFLOW = "OVERFLOW"      # n+1 slot mein bacha hua paisa
    OVERFLOW_TRIM = "OVERFLOW_TRIM"


@dataclass(frozen=True)
class Position:
    """Dhan holdings API se aaya ek scrip."""
    symbol: str
    security_id: str
    total_qty: int
    available_qty: int      # DP-free. SELL sirf isi ke against ho sakta hai.
    avg_price: float = 0.0

    @property
    def sellable(self) -> int:
        # data integrity: available should never exceed total
        # if it does, cap to total but caller should warn via planner
        if self.available_qty > self.total_qty:
            return max(0, self.total_qty)
        return max(0, min(self.total_qty, self.available_qty))

    @property
    def has_data_issue(self) -> bool:
        return self.available_qty > self.total_qty or self.available_qty < 0 or self.total_qty < 0


@dataclass(frozen=True)
class TargetName:
    """Watchlist ki ek row.

    isin / ref_ltp / market_cap optional hain -- Trendlyne jaise screener
    export mein ye milte hain aur bahut kaam ke hain:
      * isin      -> symbol rename hone par bhi sahi scrip milta hai
      * ref_ltp   -> CSV baasi (stale) hai ya nahi, ye pakad leta hai
      * market_cap-> microcap/liquidity warning
    """
    rank: int
    symbol: str
    isin: Optional[str] = None
    name: Optional[str] = None
    ref_ltp: Optional[float] = None
    market_cap_cr: Optional[float] = None


@dataclass(frozen=True)
class CircuitInfo:
    """Ek scrip ka price band. Dhan ke /marketfeed/quote se aata hai.

    Circuit par lagi hui scrip mein koi counterparty hi nahi hota:
    upper circuit par BUY nahi bharega, lower circuit par SELL nahi bharega.
    """
    symbol: str
    ltp: float
    upper: float = 0.0
    lower: float = 0.0
    prev_close: float = 0.0
    volume: int = 0            # aaj ab tak ki traded quantity

    @property
    def traded_value(self) -> float:
        """Aaj ab tak kitne rupaye ka trade hua. Impact cost ka sabse
        seedha proxy -- tumhara order iske saamne kitna bada hai."""
        return self.volume * self.ltp

    @property
    def at_upper(self) -> bool:
        # tighter 0.2% tolerance - avoid false positives from 0.5% buffer
        return bool(self.upper) and self.ltp >= self.upper * 0.998

    @property
    def at_lower(self) -> bool:
        return bool(self.lower) and self.ltp <= self.lower * 1.002

    @property
    def band_pct(self) -> Optional[float]:
        """Band ki chaudai as % of prev close (2 / 5 / 10 / 20 typical).
        Chhota band = surveillance ya illiquid scrip."""
        # prev_close must be valid - don't invent band from ltp fallback
        if not (self.upper and self.lower and self.prev_close and self.prev_close > 0):
            return None
        base = self.prev_close
        # use (upper-lower)/base as full width; for symmetric this is 2x upper-base
        # but we return half-width (upper-base)/base to keep narrow_band_warn_pct compat
        return (self.upper - base) / base * 100


@dataclass
class PlannedOrder:
    symbol: str
    security_id: str
    side: Side
    qty: int
    ref_price: float           # LTP jis par plan bana
    reason: Reason
    limit_price: Optional[float] = None
    note: str = ""

    @property
    def value(self) -> float:
        return self.qty * self.ref_price

    def correlation_id(self, run_id: str) -> str:
        """Idempotency key. Dhan ki limit 30 chars -- isliye hash se safe rakha.

        Pehle truncate karta tha -> RELIANCE vs RELIANCEPP collision.
        Ab long symbol pe hash suffix lagata hai taaki unique rahe.
        """
        cid = f"{run_id}-{self.side.value[0]}{self.symbol}"
        if len(cid) <= 30:
            return cid
        # keep prefix + 6-char hash to avoid collision within 30 limit
        h = hashlib.sha1(cid.encode()).hexdigest()[:6]
        # run_id prefix (up to 17) + "-" + side(1) + hash => ~24, rest for symbol truncated
        keep = 30 - 1 - 6  # symbol part truncated + hash
        # preserve run_id + side char fully, truncate symbol
        prefix = f"{run_id}-{self.side.value[0]}"
        trunc_sym = self.symbol[: max(0, 30 - len(prefix) - 1 - 6)]
        return f"{prefix}{trunc_sym}-{h}"[:30]


@dataclass
class Skipped:
    symbol: str
    reason: str


@dataclass
class Plan:
    run_id: str
    nav: float
    free_cash: float
    slice_value: float          # NAV_investable / n
    orders: list[PlannedOrder] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    # True = poora portfolio bechne wala plan (normal rebalance nahi)
    is_liquidation: bool = False
    # Plan kab bana (epoch seconds). Purana plan purane prices par bana hota
    # hai -- uspar LIMIT order lagana khatarnaak hai.
    created_ts: float = field(default_factory=time.time)

    # --- deploy budget --------------------------------------------------
    # User har baar poori capital market mein nahi lagana chahta. Ye do
    # field batate hain ki is plan ke baad stocks mein KITNA paisa hona
    # chahiye aur wo number aaya kahan se.
    target_equity: float = 0.0
    deploy_label: str = "poora"

    @property
    def cash_after(self) -> float:
        """Plan ke baad kitna paisa cash mein rahega (approx).
        Actual quantized orders ke baad leftover alag ho sakta hai,
        par target_equity hi budgeted cash hai."""
        # Use actual order values if orders exist for more accurate estimate?
        # Keep target based for consistency, but ensure not negative.
        return max(0.0, self.nav - self.target_equity)

    @property
    def age_sec(self) -> float:
        # created_ts 0 or None means very old / invalid - don't hide staleness
        if not self.created_ts:
            return 999999.0
        return max(0.0, time.time() - self.created_ts)

    # --- derived views -------------------------------------------------
    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side is Side.SELL]

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side is Side.BUY]

    @property
    def sell_value(self) -> float:
        return sum(o.value for o in self.sells)

    @property
    def buy_value(self) -> float:
        return sum(o.value for o in self.buys)

    @property
    def turnover_pct(self) -> float:
        """Two-way turnover -- cost estimate ke liye sahi metric."""
        if self.nav <= 0:
            return 0.0
        return (self.sell_value + self.buy_value) / self.nav

    @property
    def churn_pct(self) -> float:
        """Portfolio ka kitna hissa asal mein GHOOM raha hai.

        Risk gate isi par lagta hai, turnover_pct par nahi. Kyunki pehle
        run mein poora cash deploy hota hai -> turnover 100% -> plan block
        ho jaata, jabki churn zero hai (kuch becha hi nahi).
        """
        if self.nav <= 0:
            return 0.0
        return self.sell_value / self.nav

    @property
    def is_executable(self) -> bool:
        return not self.blockers and bool(self.orders)

    def ordered(self) -> list[PlannedOrder]:
        """SELL pehle, BUY baad mein. Ye sequence non-negotiable hai --
        buy ke paise sell se aate hain."""
        return self.sells + self.buys


@dataclass
class CostEstimate:
    brokerage: float = 0.0        # Dhan delivery = 0
    stt: float = 0.0
    txn_charges: float = 0.0
    stamp_duty: float = 0.0
    sebi_fees: float = 0.0
    gst: float = 0.0
    dp_charges: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.txn_charges +
                self.stamp_duty + self.sebi_fees + self.gst + self.dp_charges)
