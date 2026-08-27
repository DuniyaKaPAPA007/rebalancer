"""Plan ko insaan ke padhne layak banata hai. Yahi approval screen hai."""
from __future__ import annotations

from typing import Mapping

from .models import Plan, Reason, Side
from .planner import estimate_costs

_ARROW = {Reason.EXIT: "OUT ", Reason.ENTRY: "NEW ", Reason.TOPUP: "ADD ",
          Reason.TRIM: "TRIM", Reason.OVERFLOW: "n+1 ", Reason.OVERFLOW_TRIM: "n+1-"}


def _inr(x: float) -> str:
    """Indian comma format: 1,74,800 (western 174,800 nahi)."""
    neg = x < 0
    d = f"{abs(x):.0f}"
    if len(d) <= 3:
        return ("-" if neg else "") + d
    head, tail = d[:-3], d[-3:]
    groups: list[str] = []
    while len(head) > 2:                      # aakhri 3 ke baad 2-2 ke group
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if neg else "") + ",".join(groups + [tail])


def render(plan: Plan, cfg: Mapping) -> str:
    L: list[str] = []
    w = 74
    L.append("=" * w)
    L.append(f"  REBALANCE PLAN   {plan.run_id}")
    L.append("=" * w)
    L.append(f"  NAV            Rs. {_inr(plan.nav):>14}")
    L.append(f"  Free cash      Rs. {_inr(plan.free_cash):>14}")
    te = getattr(plan, "target_equity", 0.0)
    if te and not getattr(plan, "is_liquidation", False):
        L.append(f"  Stocks mein    Rs. {_inr(te):>14}   "
                 f"({getattr(plan, 'deploy_label', 'poori capital')})")
        L.append(f"  Cash mein      Rs. {_inr(max(0.0, plan.nav - te)):>14}")
    L.append(f"  Target / stock Rs. {_inr(plan.slice_value):>14}   "
             f"({cfg['portfolio']['n_stocks']} stocks barabar)")
    L.append("")

    if plan.blockers:
        L.append("  !! BLOCKED -- ye plan execute nahi hoga !!")
        for b in plan.blockers:
            L.append(f"     x {b}")
        L.append("")

    if not plan.orders:
        L.append("  Koi order nahi -- portfolio pehle se target par hai.")
        L.append("=" * w)
        return "\n".join(L)

    def table(title: str, orders, total: float) -> None:
        if not orders:
            return
        L.append(f"  {title}")
        L.append(f"  {'S.NO':>4} {'':4} {'SYMBOL':<14}{'QTY':>7} {'PRICE':>10} {'VALUE':>13}   NOTE")
        L.append("  " + "-" * (w - 4))
        for idx, o in enumerate(orders, 1):
            L.append(f"  {idx:>4} {_ARROW[o.reason]} {o.symbol:<14}{o.qty:>7} "
                     f"{o.ref_price:>10,.2f} {_inr(o.value):>13}   {o.note}")
        L.append(f"  {'':4} {'':4} {'':<14}{'':>7} {'TOTAL':>10} {_inr(total):>13}")
        L.append("")

    table(f"SELL  ({len(plan.sells)} orders)", plan.sells, plan.sell_value)
    table(f"BUY   ({len(plan.buys)} orders)", plan.buys, plan.buy_value)

    # ---- cost -------------------------------------------------------
    e = estimate_costs(plan.buy_value, plan.sell_value,
                       len({o.symbol for o in plan.sells}), cfg)
    n_sell_scrips = len({o.symbol for o in plan.sells})
    L.append("  ESTIMATED COSTS")
    for label, amt in (
        ("STT (0.1% dono taraf)", e.stt),
        (f"DP charges ({n_sell_scrips} scrip sell)", e.dp_charges),
        ("Stamp duty + txn + SEBI + GST",
         e.stamp_duty + e.txn_charges + e.sebi_fees + e.gst),
    ):
        L.append(f"     {label:<32} Rs. {_inr(amt):>10}")
    L.append(f"     {'TOTAL':<32} Rs. {_inr(e.total):>10}"
             f"   ({e.total / plan.nav * 100:.2f}% of NAV)")
    L.append("")
    L.append(f"  Turnover: {plan.turnover_pct * 100:.1f}% of NAV")
    if plan.turnover_pct > 0:
        L.append(f"  Is rate par saal bhar mein ~{e.total * 52 / plan.nav * 100:.1f}% "
                 f"NAV sirf charges mein jaayegi (slippage aur STCG alag).")
    L.append("")
    # Minimum capital
    mr = getattr(plan, "_min_required", None)
    if mr:
        L.append(f"  MINIMUM CAPITAL (har stock me 1 valid order >=Rs.{mr['min_trade_val']:.0f}):")
        L.append(f"    Slice min Rs.{_inr(mr['min_slice']):>10} each  -> Investable Rs.{_inr(mr['min_investable']):>10} -> NAV Rs.{_inr(mr['min_nav']):>10}")
        if plan.nav < mr['min_nav'] - 1:
            miss = mr['n'] - len(plan.buys)
            L.append(f"    CAPITAL KAM: Aapka NAV Rs.{_inr(plan.nav)} < min Rs.{_inr(mr['min_nav'])} -> {miss} stocks miss (8/10 jaisa)")
        else:
            L.append(f"    Capital kaafi hai: NAV Rs.{_inr(plan.nav)} >= min Rs.{_inr(mr['min_nav'])} -> sab {mr['n']} buy banenge")
        # per-stock detail up to 3
        for ps in mr['per_stock'][:3]:
            L.append(f"      {ps['symbol']:<14} price {ps['price']:>8,.2f} x{ps['min_qty']:>3} = Rs.{_inr(ps['min_value']):>8}")
        if len(mr['per_stock'])>3:
            L.append(f"      ... +{len(mr['per_stock'])-3} more")
        L.append("")

    if plan.skipped:
        L.append("  SKIPPED")
        for s in plan.skipped:
            L.append(f"     - {s.symbol}: {s.reason}")
        L.append("")

    if plan.warnings:
        L.append("  WARNINGS")
        for wn in plan.warnings:
            L.append(f"     ! {wn}")
        L.append("")

    L.append("=" * w)
    if plan.is_executable:
        L.append(f"  Execute karne ke liye:  python -m rebalancer.cli execute "
                 f"--run-id {plan.run_id} --approve")
    L.append("=" * w)
    return "\n".join(L)


def render_reconciliation(rec: dict) -> str:
    if "positions" not in rec:
        return f"  Reconciliation: {rec}"
    L = ["", "  POST-RUN PORTFOLIO", f"  NAV Rs. {_inr(rec['nav'])}", ""]
    L.append(f"  {'S.NO':>4} {'SYMBOL':<14}{'QTY':>7} {'VALUE':>13} {'WEIGHT':>9} {'DRIFT':>9}")
    L.append("  " + "-" * 60)
    for idx, p in enumerate(rec["positions"], 1):
        flag = " <-- check" if abs(p["drift_vs_target_pct"]) > 20 else ""
        L.append(f"  {idx:>4} {p['symbol']:<14}{p['qty']:>7} {_inr(p['value']):>13} "
                 f"{p['weight_pct']:>8.1f}% {p['drift_vs_target_pct']:>+8.1f}%{flag}")
    return "\n".join(L)
