"""HAFTE-DAR-HAFTE stress -- 52 hafte x 60 combos, sequential.\n\nChalao:  python tests/stress_weeks.py\n"""
import copy, random, sys, math
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan, build_liquidation_plan, estimate_costs

BASE = cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml')
BASE["risk"]["max_single_order_value_inr"] = 10_00_00_000
BASE["risk"]["max_turnover_pct"] = 9.9
BASE["risk"]["min_market_cap_cr"] = 0

UNIVERSE = [f"S{i:03d}" for i in range(60)]

class Book:
    """Nakli demat + bank. Fills yahan apply hote hain."""
    def __init__(s, cash):
        s.cash = float(cash)
        s.pos = {}           # sym -> [qty, avg]
        s.unsettled = {}     # sym -> qty (aaj kharide, kal settle)
        s.realised = 0.0
    def positions(s):
        return [Position(symbol=k, security_id=f"H{k}", total_qty=v[0],
                         available_qty=max(0, v[0] - s.unsettled.get(k,0)),
                         avg_price=v[1])
                for k,v in sorted(s.pos.items()) if v[0] > 0]
    def value(s, px):
        return sum(v[0]*px[k] for k,v in s.pos.items() if v[0] > 0)
    def settle_day(s):
        s.unsettled = {}
    def apply(s, order, fill_qty, fill_px, cfg):
        if fill_qty <= 0: return
        val = fill_qty * fill_px
        c = cfg["costs"]
        if order.side is Side.SELL:
            cost = val*float(c["est_sell_cost_pct"]) + float(c["dp_charge_per_scrip_inr"])
            s.cash += val - cost
            q, avg = s.pos[order.symbol]
            s.realised += (fill_px - avg) * fill_qty
            s.pos[order.symbol][0] = q - fill_qty
            assert s.pos[order.symbol][0] >= 0, "OVERSELL!"
        else:
            cost = val*float(c["est_buy_cost_pct"])
            s.cash -= val + cost
            if order.symbol in s.pos:
                q, avg = s.pos[order.symbol]
                nq = q + fill_qty
                s.pos[order.symbol] = [nq, (q*avg + val)/nq]
            else:
                s.pos[order.symbol] = [fill_qty, fill_px]
            s.unsettled[order.symbol] = s.unsettled.get(order.symbol,0) + fill_qty

def run(weeks=52, seed=1, n_names=11, start_cash=10_00_000,
        fill_rate=1.0, price_vol=0.05, unsettle_p=0.0, verbose=False):
    rng = random.Random(seed)
    cfg = copy.deepcopy(BASE)
    cfg["portfolio"]["n_stocks"] = "auto"
    cfg["portfolio"]["exit_rank_threshold"] = "auto"
    px = {s: round(rng.uniform(40, 2500), 2) for s in UNIVERSE}
    book = Book(start_cash)
    issues = []
    nav_hist = []

    for wk in range(weeks):
        # prices hilte hain
        for s in UNIVERSE:
            px[s] = max(1.0, round(px[s] * (1 + rng.gauss(0, price_vol)), 2))
        book.settle_day()

        names = rng.sample(UNIVERSE, n_names)
        wl = [TargetName(rank=i+1, symbol=s, ref_ltp=px[s], market_cap_cr=25000.0)
              for i,s in enumerate(names)]
        held = book.positions()
        allsym = set(names) | {h.symbol for h in held}
        sec = {s: f"H{s}" for s in allsym}
        ltp = {s: px[s] for s in allsym}
        circ = {s: CircuitInfo(symbol=s, ltp=px[s], upper=px[s]*1.2, lower=px[s]*0.8,
                               prev_close=px[s], volume=int(5_00_00_000/px[s]))
                for s in allsym}

        nav_before = book.value(px) + book.cash
        nav_hist.append(nav_before)
        plan = build_plan(run_id=f"W{wk}", holdings=held, free_cash=book.cash,
                          watchlist=wl, ltp=ltp, security_ids=sec, cfg=cfg,
                          circuit=circ)
        if plan.blockers:
            issues.append((wk, "BLOCKED", plan.blockers[:1]))
            continue

        # ---- invariants: plan banne ke turant baad
        if abs(plan.nav - nav_before) > 1.0:
            issues.append((wk, "NAV_MISMATCH", f"{plan.nav:.0f} vs {nav_before:.0f}"))
        sells = [o for o in plan.orders if o.side is Side.SELL]
        buys  = [o for o in plan.orders if o.side is Side.BUY]
        avail = {h.symbol: h.available_qty for h in held}
        for o in sells:
            if o.qty > avail.get(o.symbol, 0):
                issues.append((wk, "OVERSELL_PLAN", f"{o.symbol} {o.qty}>{avail.get(o.symbol,0)}"))
        need = sum(o.value for o in buys)*(1+cfg["costs"]["est_buy_cost_pct"])
        got  = book.cash + sum(o.value for o in sells)*(1-cfg["costs"]["est_sell_cost_pct"]) \
               - cfg["costs"]["dp_charge_per_scrip_inr"]*len(sells)
        if need > got + 1.0:
            issues.append((wk, "CASH_SHORT", f"{need:.0f} > {got:.0f}"))
        both = {o.symbol for o in buys} & {o.symbol for o in sells}
        if both: issues.append((wk, "WASH", sorted(both)))

        # ---- execute: SELL pehle, phir BUY (app ka asli order)
        for o in sells:
            q = o.qty if rng.random() <= fill_rate else int(o.qty*rng.uniform(0,0.9))
            book.apply(o, q, px[o.symbol], cfg)
        if book.cash < -1.0:
            issues.append((wk, "CASH_NEG_AFTER_SELL", f"{book.cash:.0f}"))
        for o in buys:
            if book.cash < o.value*(1+cfg["costs"]["est_buy_cost_pct"]):
                continue            # executor bhi yahi karta hai
            q = o.qty if rng.random() <= fill_rate else int(o.qty*rng.uniform(0,0.9))
            book.apply(o, q, px[o.symbol], cfg)
        if book.cash < -1.0:
            issues.append((wk, "CASH_NEG_AFTER_BUY", f"{book.cash:.0f}"))
        for s,v in book.pos.items():
            if v[0] < 0: issues.append((wk, "NEG_QTY", f"{s} {v[0]}"))

    final = book.value(px) + book.cash
    return dict(final_nav=final, start=start_cash, weeks=weeks,
                issues=issues, cash=book.cash, n_pos=len([1 for v in book.pos.values() if v[0]>0]),
                nav_hist=nav_hist)

if __name__ == "__main__":
    print(f"{'seed':>5}{'weeks':>7}{'fill':>7}{'vol':>6}{'unsettled':>11}"
          f"{'final NAV':>13}{'cash':>11}{'pos':>5}{'issues':>8}")
    print("-"*80)
    allbad = []
    for seed in range(1, 16):
        for fill, vol in [(1.0,0.05),(1.0,0.15),(0.85,0.08),(0.6,0.20)]:
            r = run(weeks=52, seed=seed, fill_rate=fill, price_vol=vol)
            bad = r["issues"]
            allbad += [(seed,fill,vol,*b) for b in bad]
            flag = "" if not bad else f"  <-- {bad[0][1]}"
            print(f"{seed:>5}{52:>7}{fill:>7.2f}{vol:>6.2f}{'-':>11}"
                  f"{r['final_nav']:>13,.0f}{r['cash']:>11,.0f}{r['n_pos']:>5}{len(bad):>8}{flag}")
    print("\n" + "="*80)
    from collections import Counter
    c = Counter(b[4] for b in allbad)
    print("  ISSUE SUMMARY:", dict(c) if c else "KOI ISSUE NAHI")
    for b in allbad[:8]: print("   ", b)
