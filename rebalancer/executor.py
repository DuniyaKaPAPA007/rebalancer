"""
Order execution.

Do sakht niyam jo kabhi todne nahi hain:

  1. SELL pehle, BUY baad mein -- aur SELL ke fill confirm hone ke baad.
     Buy ka paisa sell se aata hai. Dono ek saath bhejoge toh
     "insufficient funds" reject milega.

  2. Har order ka correlationId. Network timeout ke baad blind retry
     duplicate order bana deta hai -- yaani double paisa. correlationId
     se pata chal jaata hai ki order pehle hi ja chuka hai.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Mapping

from .dhan import DhanClient, DhanError
from .models import Plan, PlannedOrder, Side
from .store import Store

log = logging.getLogger(__name__)

TERMINAL_OK = {"TRADED"}
TERMINAL_BAD = {"REJECTED", "CANCELLED", "EXPIRED"}
PENDING = {"TRANSIT", "PENDING", "PART_TRADED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Executor:
    def __init__(self, client: DhanClient, store: Store, cfg: Mapping,
                 dry_run: bool = True):
        self.c, self.db, self.cfg = client, store, cfg
        self.dry = dry_run
        self.x = cfg["execution"]
        self.seg = cfg["dhan"]["exchange_segment"]
        self.prod = cfg["dhan"]["product_type"]

    # ------------------------------------------------------------------
    def _check_age(self, plan: Plan) -> None:
        """Purana plan purane prices par bana hai.

        LIMIT price plan ke waqt ke LTP se nikla tha. Agar tab se stock
        hil gaya, order ya toh bharega hi nahi, ya bahut bure bhaav par
        bharega. Qty bhi purane NAV se nikli hai.

        Isliye zyada purana plan execute karne se pehle rok dete hain --
        naya plan banana 5 second ka kaam hai.
        """
        limit_min = float(self.x.get("max_plan_age_min", 15) or 0)
        if limit_min <= 0:
            return
        age_min = plan.age_sec / 60.0
        if age_min > limit_min:
            raise RuntimeError(
                f"Ye plan {age_min:.0f} MINUTE purana hai (limit "
                f"{limit_min:.0f} minute). Tab ke prices par bane LIMIT "
                f"order ab shayad bharenge hi nahi, aur qty bhi purane NAV "
                f"se nikli hai.\n\nNaya plan banao -- 5 second lagega. "
                f"(Limit badalni ho toh config: execution.max_plan_age_min)")

    def run(self, plan: Plan) -> dict:
        if plan.blockers:
            raise RuntimeError("Plan blocked hai -- execute nahi kar sakte:\n  " +
                               "\n  ".join(plan.blockers))

        if not self.dry:
            self._check_age(plan)
        self.db.set_status(plan.run_id, "EXECUTING")
        result = {"run_id": plan.run_id, "sells": [], "buys": [],
                  "failed": [], "dry_run": self.dry}

        # ---------- PHASE 1: SELL ---------------------------------------
        sells = plan.sells
        if sells:
            log.info("PHASE 1 -- %d SELL orders", len(sells))
            result["sells"] = self._place_batch(plan.run_id, sells, result)
            self._await_fills(plan.run_id, result["sells"])

            gap = int(self.x["phase_gap_sec"])
            if gap and not self.dry:
                log.info("Funds release hone ke liye %ds ruk rahe hain...", gap)
                time.sleep(gap)

        # ---------- PHASE 2: BUY ----------------------------------------
        buys = plan.buys
        if buys:
            # Cash dobara check karo -- sell partial fill hua ho sakta hai,
            # ya price hil gaya ho. Plan ke bharose andha buy mat karo.
            buys = self._fit_to_available_cash(buys, plan)
            log.info("PHASE 2 -- %d BUY orders", len(buys))
            result["buys"] = self._place_batch(plan.run_id, buys, result)
            self._await_fills(plan.run_id, result["buys"])

        self.db.set_status(plan.run_id, "DONE" if not result["failed"] else "DONE_WITH_ERRORS")
        result["reconciliation"] = self.reconcile(plan)
        return result

    # ------------------------------------------------------------------
    def _fit_to_available_cash(self, buys: list[PlannedOrder],
                               plan: Plan) -> list[PlannedOrder]:
        if self.dry:
            return buys
        try:
            cash = self.c.available_cash()
        except DhanError as e:
            log.warning("Funds fetch nahi hua (%s) -- plan ke hisaab se chal rahe hain", e)
            return buys

        need = sum(o.value for o in buys)
        if need <= cash:
            return buys

        log.warning("Cash Rs.%.0f hai, chahiye Rs.%.0f -- buys kaat rahe hain.",
                    cash, need)
        kept, spent = [], 0.0
        for o in buys:                      # plan mein rank order preserved hai
            if spent + o.value <= cash:
                kept.append(o)
                spent += o.value
            else:
                affordable = int((cash - spent) // o.ref_price)
                if affordable > 0:
                    o.qty = affordable
                    kept.append(o)
                    spent += o.value
                    break
        plan.warnings.append(
            f"Execution ke waqt cash kam nikla -- {len(buys) - len(kept)} buy skip hue.")
        return kept

    # ------------------------------------------------------------------
    def _place_batch(self, run_id: str, orders: list[PlannedOrder],
                     result: dict) -> list[dict]:
        placed = []
        for o in orders:
            cid = o.correlation_id(run_id)

            # guard 1: hamari apni DB
            prev = self.db.order_already_sent(cid)
            if prev and prev.get("broker_order_id"):
                log.warning("%s pehle hi bhej chuke hain (%s) -- skip",
                            cid, prev["broker_order_id"])
                placed.append({"order": o, "order_id": prev["broker_order_id"],
                               "cid": cid, "resumed": True})
                continue

            base = dict(run_id=run_id, correlation_id=cid, symbol=o.symbol,
                        security_id=o.security_id, side=o.side.value,
                        reason=o.reason.value, planned_qty=o.qty,
                        limit_price=o.limit_price, placed_at=_now(),
                        updated_at=_now())

            if self.dry:
                self.db.record_order(**base, status="DRY_RUN")
                placed.append({"order": o, "order_id": None, "cid": cid, "dry": True})
                continue

            try:
                # guard 2: broker ki apni memory (DB corrupt/mit gaya ho toh)
                resp = self.c.place_order(
                    security_id=o.security_id, side=o.side.value, qty=o.qty,
                    exchange_segment=self.seg, product_type=self.prod,
                    order_type=self.x["order_type"],
                    price=o.limit_price or o.ref_price,
                    correlation_id=cid)
                oid = str(resp.get("orderId") or resp.get("order_id") or "")
                self.db.record_order(**base, broker_order_id=oid,
                                     status=resp.get("orderStatus", "TRANSIT"))
                placed.append({"order": o, "order_id": oid, "cid": cid})
                log.info("%s %s x%d -> %s", o.side.value, o.symbol, o.qty, oid)
            except DhanError as e:
                self.db.record_order(**base, status="FAILED", error=str(e))
                result["failed"].append({"symbol": o.symbol, "side": o.side.value,
                                         "error": str(e)})
                log.error("%s %s FAIL: %s", o.side.value, o.symbol, e)
        return placed

    # ------------------------------------------------------------------
    def _await_fills(self, run_id: str, placed: list[dict]) -> None:
        """Fill ka intezaar. Partial fill par timeout ke baad baaki cancel."""
        if self.dry:
            return
        live = [p for p in placed if p.get("order_id")]
        if not live:
            return

        timeout = int(self.x["fill_wait_timeout_sec"])
        poll = int(self.x["fill_poll_interval_sec"])
        fallback_at = int(self.x["market_fallback_after_sec"])
        start = time.monotonic()
        converted: set[str] = set()

        while live and (time.monotonic() - start) < timeout:
            time.sleep(poll)
            elapsed = time.monotonic() - start
            still: list[dict] = []

            for p in live:
                try:
                    st = self.c.order(p["order_id"])
                except DhanError as e:
                    log.warning("status fetch fail %s: %s", p["order_id"], e)
                    still.append(p)
                    continue

                status = (st.get("orderStatus") or "").upper()
                filled = int(st.get("filledQty") or st.get("filled_qty") or 0)
                self.db.record_order(
                    run_id=run_id, correlation_id=p["cid"], symbol=p["order"].symbol,
                    side=p["order"].side.value, planned_qty=p["order"].qty,
                    broker_order_id=p["order_id"], filled_qty=filled,
                    avg_fill_price=float(st.get("averageTradedPrice") or 0),
                    status=status, updated_at=_now())

                if status in TERMINAL_OK:
                    log.info("FILLED %s %s x%d", p["order"].side.value,
                             p["order"].symbol, filled)
                    continue
                if status in TERMINAL_BAD:
                    log.error("%s %s -> %s (%s)", p["order"].side.value,
                              p["order"].symbol, status,
                              st.get("omsErrorDescription", ""))
                    continue

                # abhi tak pending -> MARKET mein convert karna hai?
                if (fallback_at and elapsed > fallback_at
                        and p["order_id"] not in converted):
                    remaining = p["order"].qty - filled
                    if remaining > 0:
                        try:
                            self.c.modify_to_market(p["order_id"], remaining)
                            converted.add(p["order_id"])
                            log.warning("%s %s: %ds mein fill nahi hua -> MARKET",
                                        p["order"].side.value, p["order"].symbol,
                                        int(elapsed))
                        except DhanError as e:
                            log.warning("market convert fail: %s", e)
                still.append(p)
            live = still

        # timeout ke baad jo bacha, use cancel karo -- adhoora order
        # agle hafte ke plan ko kharab karta hai
        for p in live:
            try:
                self.c.cancel(p["order_id"])
                log.warning("TIMEOUT -> cancelled %s %s",
                            p["order"].side.value, p["order"].symbol)
                self.db.record_order(run_id=run_id, correlation_id=p["cid"],
                                     symbol=p["order"].symbol,
                                     side=p["order"].side.value,
                                     planned_qty=p["order"].qty,
                                     broker_order_id=p["order_id"],
                                     status="CANCELLED_TIMEOUT", updated_at=_now())
            except DhanError as e:
                log.error("cancel fail %s: %s", p["order_id"], e)

    # ------------------------------------------------------------------
    def reconcile(self, plan: Plan) -> dict:
        """Run ke baad: asli holdings vs target. Drift yahin pakda jaata hai."""
        if self.dry:
            return {"skipped": "dry run"}
        try:
            holdings = self.c.holdings()
            ltp = self.c.ltp([h.security_id for h in holdings]) if holdings else {}
        except DhanError as e:
            return {"error": str(e)}

        rows, nav = [], 0.0
        for h in holdings:
            px = ltp.get(h.security_id, 0.0)
            nav += h.total_qty * px
            rows.append((h.symbol, h.total_qty, px))
        self.db.snapshot(plan.run_id, "AFTER", rows, _now())

        out = []
        for sym, qty, px in rows:
            val = qty * px
            out.append({
                "symbol": sym, "qty": qty, "value": round(val, 2),
                "weight_pct": round(val / nav * 100, 2) if nav else 0,
                "drift_vs_target_pct": round(
                    (val - plan.slice_value) / plan.slice_value * 100, 1)
                if plan.slice_value else 0,
            })
        out.sort(key=lambda r: -r["value"])
        return {"nav": round(nav, 2), "positions": out}
