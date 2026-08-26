"""
Planner ke tests. Yahi wo jagah hai jahan paise wala logic verify hota hai.
Sab kuch deterministic hai -- na network, na clock, na randomness.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rebalancer.models import (CircuitInfo, Position, Reason, Side,  # noqa: E402
                               TargetName)
from rebalancer.planner import build_plan  # noqa: E402


# ----------------------------------------------------------------------
# Base config: costs ZERO rakhe hain taaki tests logic test karein,
# rounding nahi. Cost ka apna alag test neeche hai.
CFG = {
    "portfolio": {"n_stocks": 10, "exit_rank_threshold": 10,
                  "drift_band_pct": 0.0, "cash_reserve_pct": 0.0,
                  "use_overflow_slot": True},
    "costs": {"min_trade_value_inr": 500, "min_trade_pct_of_slice": 0.03,
              "est_sell_cost_pct": 0.0, "est_buy_cost_pct": 0.0,
              "dp_charge_per_scrip_inr": 0.0},
    "execution": {"limit_buffer_pct": 0.003},
    "risk": {"max_turnover_pct": 2.0, "max_single_order_value_inr": 10_000_000,
             "min_price_inr": 10},
}

LIVE_COSTS = {"min_trade_value_inr": 500, "min_trade_pct_of_slice": 0.03,
              "est_sell_cost_pct": 0.0012, "est_buy_cost_pct": 0.0004,
              "dp_charge_per_scrip_inr": 14.75}

SYMS = [f"S{i}" for i in range(1, 13)]          # S1..S12
PRICE = {s: 100.0 for s in SYMS}                # sab Rs.100 -- math saaf rahe


def wl(symbols):
    return [TargetName(rank=i + 1, symbol=s) for i, s in enumerate(symbols)]


def plan_for(holdings, cash, symbols=None, prices=None, cfg=None, circuit=None):
    """securityId aur LTP har us symbol ke liye auto-bana dete hain jo
    holdings ya watchlist mein hai -- warna planner (sahi hi) block kar dega."""
    watch = wl(SYMS[:11] if symbols is None else symbols)   # [] bhi valid hai
    universe = {t.symbol for t in watch} | {h.symbol for h in holdings}
    sec = {s: f"sec-{s}" for s in universe}
    px = {s: 100.0 for s in universe}
    px.update(prices or {})
    return build_plan(
        run_id="T1",
        holdings=holdings,
        free_cash=cash,
        watchlist=watch,
        ltp=px,
        security_ids=sec,
        cfg=cfg or CFG,
        circuit=circuit,
    )


def by_symbol(plan):
    return {o.symbol: o for o in plan.orders}


# ----------------------------------------------------------------------
#  1. Khaali portfolio -> 10 barabar entries + n+1 mein bacha paisa
# ----------------------------------------------------------------------
def test_fresh_portfolio_equal_weight_and_overflow():
    p = plan_for([], cash=100_000)

    assert not p.blockers
    assert p.nav == 100_000
    assert p.slice_value == 10_000

    entries = [o for o in p.orders if o.reason is Reason.ENTRY]
    assert len(entries) == 10
    assert all(o.qty == 100 for o in entries)     # 10000 / 100
    assert all(o.side is Side.BUY for o in entries)

    # 100 shares x 10 stocks x Rs.100 = Rs.100,000 -> kuch nahi bacha
    assert not [o for o in p.orders if o.reason is Reason.OVERFLOW]


def test_overflow_slot_gets_the_remainder():
    # slice 10,000 par har share Rs.300 -> 33 share = 9,900, Rs.100 x10 bachta hai
    p = plan_for([], cash=100_000, prices={s: 300.0 for s in SYMS})

    entries = [o for o in p.orders if o.reason is Reason.ENTRY]
    assert all(o.qty == 33 for o in entries)      # 9,900 each = 99,000

    ov = [o for o in p.orders if o.reason is Reason.OVERFLOW]
    assert len(ov) == 1 and ov[0].symbol == "S11"
    assert ov[0].qty == 3                          # ~1,000 bacha / 300


def test_overflow_skipped_when_leftover_too_small():
    """Bacha paisa hai par n+1 ka ek share bhi nahi aata -> cash rakho,
    chup-chaap ignore mat karo."""
    prices = {s: 300.0 for s in SYMS}
    prices["S11"] = 50_000.0
    p = plan_for([], cash=100_000, prices=prices)

    assert not [o for o in p.orders if o.reason is Reason.OVERFLOW]
    assert any(s.symbol == "S11" for s in p.skipped)


# ----------------------------------------------------------------------
#  2. CARRY-OVER -- ye hai asli sawaal ka jawaab
# ----------------------------------------------------------------------
def test_carryover_is_never_fully_sold_and_rebought():
    """S1 pehle se hold hai aur nayi list mein bhi hai.
    Poora bech ke dobara kharidna BILKUL nahi hona chahiye."""
    holdings = [Position("S1", "sec-S1", 100, 100)]
    p = plan_for(holdings, cash=90_000)

    orders = by_symbol(p)
    # S1 exact target par hai (100 x 100 = 10,000 = slice) -> koi order hi nahi
    assert "S1" not in orders
    assert not any(o.symbol == "S1" and o.side is Side.SELL for o in p.orders)


def test_carryover_underweight_gets_topup_only_the_delta():
    # S1: 40 share hold (Rs.4,000), target Rs.10,000 -> sirf 60 aur khareedo
    holdings = [Position("S1", "sec-S1", 40, 40)]
    p = plan_for(holdings, cash=96_000)

    o = by_symbol(p)["S1"]
    assert o.side is Side.BUY
    assert o.reason is Reason.TOPUP
    assert o.qty == 60                      # 100 - 40, poora 100 nahi


def test_carryover_overweight_gets_trimmed_not_exited():
    # S1: 300 share (Rs.30,000) jabki target Rs.10,000 -> sirf 200 becho
    holdings = [Position("S1", "sec-S1", 300, 300)]
    p = plan_for(holdings, cash=70_000)

    o = by_symbol(p)["S1"]
    assert o.side is Side.SELL
    assert o.reason is Reason.TRIM
    assert o.qty == 200                     # 300 nahi -- position bacha rehta hai


def test_dropped_name_is_fully_exited():
    # S99 list mein nahi hai -> poora nikaalo
    holdings = [Position("S99", "sec-S99", 50, 50)]
    p = plan_for(holdings, cash=95_000)

    o = by_symbol(p)["S99"]
    assert o.side is Side.SELL and o.reason is Reason.EXIT and o.qty == 50


def test_drift_band_suppresses_small_carryover_trades():
    """Band ON kar do toh thoda-bahut drift ignore ho jaata hai -- churn bachti hai."""
    cfg = {**CFG, "portfolio": {**CFG["portfolio"], "drift_band_pct": 0.25}}
    holdings = [Position("S1", "sec-S1", 112, 112)]     # 12% overweight
    p = plan_for(holdings, cash=88_800, cfg=cfg)
    assert "S1" not in by_symbol(p)                      # band ke andar -> chhodo


# ----------------------------------------------------------------------
#  3. Settlement / DP guard
# ----------------------------------------------------------------------
def test_never_sells_more_than_dp_free_quantity():
    """Unsettled shares bechna = short delivery + auction penalty.
    Kabhi nahi hona chahiye."""
    holdings = [Position("S99", "sec-S99", total_qty=100, available_qty=40)]
    p = plan_for(holdings, cash=90_000)

    o = by_symbol(p)["S99"]
    assert o.qty == 40                     # 100 nahi
    assert any("unsettled" in w for w in p.warnings)


def test_zero_sellable_is_skipped_not_ordered():
    holdings = [Position("S99", "sec-S99", total_qty=100, available_qty=0)]
    p = plan_for(holdings, cash=90_000)
    assert "S99" not in by_symbol(p)
    assert any(s.symbol == "S99" for s in p.skipped)


# ----------------------------------------------------------------------
#  4. Cash constraint
# ----------------------------------------------------------------------
def test_buys_never_exceed_available_cash():
    """Asli shortfall: Rs.50,000 ka purana naam hold hai par woh abhi
    unsettled hai (bech nahi sakte). NAV 60,000 hai lekin haath mein sirf
    10,000. Buys ko 10,000 ke andar hi rehna chahiye."""
    holdings = [Position("OLD1", "sec-OLD1", total_qty=500, available_qty=0)]
    p = plan_for(holdings, cash=10_000)

    assert p.nav == 60_000
    assert p.sell_value == 0                    # kuch bech hi nahi sakte
    assert p.buy_value <= 10_000 + 1            # capital se zyada kabhi nahi
    assert any("Cash kam tha" in w for w in p.warnings)


def test_sell_orders_come_before_buy_orders():
    holdings = [Position("S99", "sec-S99", 500, 500)]
    p = plan_for(holdings, cash=50_000)
    seq = [o.side for o in p.ordered()]
    assert seq == sorted(seq, key=lambda s: 0 if s is Side.SELL else 1)


# ----------------------------------------------------------------------
#  5. Dust filter
# ----------------------------------------------------------------------
def test_dust_trades_are_skipped():
    # S1 sirf Rs.200 underweight -> Rs.500 min se kam -> koi order nahi
    holdings = [Position("S1", "sec-S1", 98, 98)]
    p = plan_for(holdings, cash=90_200)
    assert "S1" not in by_symbol(p)


# ----------------------------------------------------------------------
#  6. Risk gates
# ----------------------------------------------------------------------
def test_turnover_cap_blocks_runaway_selling():
    cfg = {**CFG, "risk": {**CFG["risk"], "max_turnover_pct": 0.10}}
    holdings = [Position("OLD1", "sec-OLD1", 500, 500)]      # poora exit
    p = plan_for(holdings, cash=0, cfg=cfg)
    assert p.blockers and not p.is_executable


def test_first_deployment_is_not_blocked_by_turnover_cap():
    """Pehla run: saara cash deploy hota hai. Two-way turnover ~100% dikhta
    hai par churn ZERO hai (kuch becha hi nahi) -- block nahi hona chahiye."""
    cfg = {**CFG, "risk": {**CFG["risk"], "max_turnover_pct": 0.60}}
    p = plan_for([], cash=100_000, cfg=cfg)

    assert p.churn_pct == 0.0
    assert p.turnover_pct > 0.9          # two-way number abhi bhi honest hai
    assert not p.blockers
    assert p.is_executable


def test_penny_stock_guard_blocks_buys_but_never_exits():
    """Guard ka kaam hai kachra KHAREEDNE se rokna. Jo pehle se hold hai
    usse nikalne ka raasta band karna bug hai -- position phans jaayegi."""
    holdings = [Position("PENNY", "sec-PENNY", 3000, 3000)]
    p = plan_for(holdings, cash=97_000, prices={"PENNY": 1.0})

    o = by_symbol(p)["PENNY"]
    assert o.side is Side.SELL and o.reason is Reason.EXIT
    assert o.qty == 3000                        # exit hua, skip nahi


def test_penny_stock_guard_skips_buy_side():
    prices = {s: 100.0 for s in SYMS}
    prices["S3"] = 2.0                          # target list mein hai par penny
    p = plan_for([], cash=100_000, prices=prices)

    assert "S3" not in by_symbol(p)
    assert any(s.symbol == "S3" and "penny" in s.reason for s in p.skipped)


def test_missing_security_id_blocks():
    p = build_plan(run_id="T", holdings=[], free_cash=100_000,
                   watchlist=wl(SYMS[:11]), ltp=PRICE,
                   security_ids={s: f"sec-{s}" for s in SYMS[:5]}, cfg=CFG)
    assert any("securityId" in b for b in p.blockers)


# ----------------------------------------------------------------------
#  9. Adhoori watchlist (10 se kam naam)
# ----------------------------------------------------------------------
def test_partial_list_full_mode_deploys_everything():
    """4 naam aaye -> har ek ko 25%, portfolio 100% invested."""
    cfg = {**CFG, "portfolio": {**CFG["portfolio"], "partial_list_mode": "full"}}
    p = plan_for([], cash=100_000, symbols=SYMS[:4], cfg=cfg)

    assert not p.blockers
    assert p.slice_value == 25_000                # 100,000 / 4, /10 nahi
    assert len(p.buys) == 4
    assert p.buy_value == pytest.approx(100_000)  # kuch cash nahi bacha


def test_partial_list_fixed_slots_keeps_rest_in_cash():
    """Wahi 4 naam, par backtest wale mode mein -> 40% invested."""
    cfg = {**CFG, "portfolio": {**CFG["portfolio"],
                                "partial_list_mode": "fixed_slots"}}
    p = plan_for([], cash=100_000, symbols=SYMS[:4], cfg=cfg)

    assert p.slice_value == 10_000                # NAV/10
    assert p.buy_value == pytest.approx(40_000)   # baaki 60% cash


def test_single_name_week_raises_concentration_warning():
    """Sirf 1 naam = 100% ek stock mein. Ye chup-chaap nahi hona chahiye."""
    cfg = {**CFG, "portfolio": {**CFG["portfolio"], "partial_list_mode": "full"}}
    p = plan_for([], cash=100_000, symbols=SYMS[:1], cfg=cfg)

    assert p.slice_value == 100_000
    assert any("CONCENTRATION" in w for w in p.warnings)


def test_weight_cap_limits_concentration_when_set():
    cfg = {**CFG, "portfolio": {**CFG["portfolio"], "partial_list_mode": "full",
                                "max_weight_per_stock_pct": 0.25}}
    p = plan_for([], cash=100_000, symbols=SYMS[:2], cfg=cfg)

    assert p.slice_value == 25_000                # 50% nahi
    assert p.buy_value == pytest.approx(50_000)   # baaki cash
    assert any("cap" in w for w in p.warnings)


def test_empty_watchlist_blocks():
    p = plan_for([], cash=100_000, symbols=[])
    assert any("khaali" in b for b in p.blockers)


# ----------------------------------------------------------------------
#  10. Circuit detection
# ----------------------------------------------------------------------
def test_upper_circuit_buy_is_flagged():
    """Upper circuit par BUY nahi bharega -- plan mein dikhna chahiye."""
    ci = {"S1": CircuitInfo("S1", ltp=100.0, upper=100.0, lower=80.0,
                            prev_close=90.0)}
    p = plan_for([], cash=100_000, circuit=ci)
    assert any("UPPER CIRCUIT" in w and "S1" in w for w in p.warnings)


def test_lower_circuit_sell_is_flagged():
    holdings = [Position("OLD1", "sec-OLD1", 500, 500)]
    ci = {"OLD1": CircuitInfo("OLD1", ltp=100.0, upper=120.0, lower=100.0,
                              prev_close=110.0)}
    p = plan_for(holdings, cash=50_000, circuit=ci)
    assert any("LOWER CIRCUIT" in w and "OLD1" in w for w in p.warnings)


def test_narrow_band_scrip_is_flagged():
    """5% band = surveillance/illiquid. SWANDEF wala case."""
    ci = {"S2": CircuitInfo("S2", ltp=100.0, upper=105.0, lower=95.0,
                            prev_close=100.0)}
    p = plan_for([], cash=100_000, circuit=ci)
    assert any("circuit band" in w and "S2" in w for w in p.warnings)


def test_normal_band_scrip_is_not_flagged():
    ci = {"S2": CircuitInfo("S2", ltp=100.0, upper=120.0, lower=80.0,
                            prev_close=100.0)}
    p = plan_for([], cash=100_000, circuit=ci)
    assert not any("circuit band" in w for w in p.warnings)


def test_big_order_in_thin_stock_is_flagged():
    """Rs.10,000 ka order jab aaj sirf Rs.50,000 ka trade hua ho = 20%.
    Ye backtest mein kabhi nahi dikhta."""
    ci = {"S1": CircuitInfo("S1", ltp=100.0, upper=120.0, lower=80.0,
                            prev_close=100.0, volume=500)}   # Rs.50,000 traded
    p = plan_for([], cash=100_000, circuit=ci)
    w = [x for x in p.warnings if "LIQUIDITY" in x]
    assert w and "S1" in w[0]


def test_liquid_stock_is_not_flagged():
    ci = {"S1": CircuitInfo("S1", ltp=100.0, upper=120.0, lower=80.0,
                            prev_close=100.0, volume=10_000_000)}
    p = plan_for([], cash=100_000, circuit=ci)
    assert not any("LIQUIDITY" in x for x in p.warnings)


def test_no_circuit_data_is_harmless():
    p = plan_for([], cash=100_000, circuit=None)
    assert not p.blockers
    assert not any("CIRCUIT" in w for w in p.warnings)


def test_low_capital_raises_precision_warning():
    p = plan_for([], cash=100_000, prices={s: 3000.0 for s in SYMS})
    assert any("equal weight kaafi kharab" in w for w in p.warnings)


# ----------------------------------------------------------------------
#  8. Costs plug hone par bhi cash constraint tut-ta nahi
# ----------------------------------------------------------------------
def test_real_costs_keep_buys_within_budget():
    cfg = {**CFG, "costs": LIVE_COSTS,
           "portfolio": {**CFG["portfolio"], "cash_reserve_pct": 0.01}}
    holdings = [Position("OLD1", "sec-OLD1", 400, 400)]      # Rs.40,000 exit
    p = plan_for(holdings, cash=60_000, cfg=cfg)

    assert not p.blockers
    proceeds = p.sell_value * (1 - LIVE_COSTS["est_sell_cost_pct"]) \
        - LIVE_COSTS["dp_charge_per_scrip_inr"] * len(p.sells)
    assert p.buy_value <= 60_000 + proceeds + 1


# ----------------------------------------------------------------------
#  7. Full realistic reshuffle
# ----------------------------------------------------------------------
def test_full_weekly_reshuffle():
    """Pichhle hafte: S1..S8 + do naam jo ab list se bahar hain.
    Is hafte: S1..S10. Expected -> 2 exit, 8 carry-over (delta only), 2 entry."""
    holdings = [Position(s, f"sec-{s}", 100, 100) for s in SYMS[:8]]
    holdings += [Position("OLD1", "sec-OLD1", 100, 100),
                 Position("OLD2", "sec-OLD2", 100, 100)]
    p = plan_for(holdings, cash=0)

    o = by_symbol(p)
    assert o["OLD1"].reason is Reason.EXIT and o["OLD1"].qty == 100
    assert o["OLD2"].reason is Reason.EXIT and o["OLD2"].qty == 100
    # S1..S8 already exact slice par -> koi trade nahi
    for s in SYMS[:8]:
        assert s not in o, f"{s} carry-over hai, ise chhedna nahi chahiye tha"
    # S9, S10 naye
    assert o["S9"].reason is Reason.ENTRY
    assert o["S10"].reason is Reason.ENTRY
    assert p.sell_value == 20_000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ======================================================================
#  Audit ke baad jode gaye tests -- ye do bug asli mein mile the
# ======================================================================

def test_no_wash_trade_on_overflow():
    """n+1 slot ko fund ke liye becha AUR wapas khareeda -- ye nahi hona chahiye.

    Asli bug tha: OVERFLOW_TRIM 207 share bechta tha aur OVERFLOW 5008
    share usi scrip ke khareed leta tha. Do taraf ka STT, stamp, DP charge,
    dohra slippage, aur ek bekaar ka STCG event.
    """
    from rebalancer.models import Position, TargetName, CircuitInfo, Side
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["portfolio"]["n_stocks"] = 5
    cfg["portfolio"]["exit_rank_threshold"] = "auto"
    cfg["portfolio"]["use_overflow_slot"] = True
    cfg["risk"]["max_single_order_value_inr"] = 10_00_00_000

    syms = [f"T{i}" for i in range(1, 6)] + ["OV"]
    px = {s: 950.71 for s in syms}
    wl = [TargetName(rank=i + 1, symbol=s, ref_ltp=px[s], market_cap_cr=25_000.0)
          for i, s in enumerate(syms)]
    holds = [Position(symbol="OV", security_id="HOV", total_qty=500,
                      available_qty=500, avg_price=900.0)]
    circ = {s: CircuitInfo(symbol=s, ltp=px[s], upper=px[s] * 1.2,
                           lower=px[s] * .8, prev_close=px[s], volume=10_00_000)
            for s in syms}
    plan = build_plan(run_id="W", holdings=holds, free_cash=90_00_000.0,
                      watchlist=wl, ltp=px, security_ids={s: f"H{s}" for s in syms},
                      cfg=cfg, circuit=circ)

    for sym in syms:
        sides = {o.side for o in plan.orders if o.symbol == sym}
        assert not (Side.BUY in sides and Side.SELL in sides), \
            f"{sym} ka BUY aur SELL dono ban gaya -- wash trade"


def test_plan_never_asks_more_cash_than_it_has():
    """Har plan ka buy, available cash + sell proceeds ke andar hona chahiye."""
    from rebalancer.models import Position, TargetName, CircuitInfo, Side
    import copy, random
    random.seed(4)
    for it in range(300):
        cfg = copy.deepcopy(CFG)
        cfg["costs"]["est_sell_cost_pct"] = 0.0012
        cfg["costs"]["est_buy_cost_pct"] = 0.0004
        cfg["portfolio"]["n_stocks"] = random.choice(["auto", random.randint(2, 12)])
        cfg["portfolio"]["use_overflow_slot"] = random.random() < .7
        cfg["portfolio"]["drift_band_pct"] = random.choice([0.0, 0.05, 0.20])
        cfg["risk"]["max_single_order_value_inr"] = 10_00_00_000
        cfg["risk"]["max_turnover_pct"] = 9.9

        n = random.randint(2, 18)
        syms = [f"W{i}" for i in range(n)] + [f"X{i}" for i in range(4)]
        px = {s: round(random.uniform(15, 3000), 2) for s in syms}
        wl = [TargetName(rank=i + 1, symbol=s, ref_ltp=px[s], market_cap_cr=25_000.0)
              for i, s in enumerate(syms[:n])]
        holds = []
        for s in random.sample(syms, random.randint(0, min(8, len(syms)))):
            q = random.randint(1, 3000)
            holds.append(Position(symbol=s, security_id=f"H{s}", total_qty=q,
                                  available_qty=q if random.random() < .8
                                  else random.randint(0, q),
                                  avg_price=px[s]))
        cash = round(random.uniform(0, 50_00_000), 2)
        circ = {s: CircuitInfo(symbol=s, ltp=px[s], upper=px[s] * 1.2,
                               lower=px[s] * .8, prev_close=px[s],
                               volume=10_00_000) for s in syms}
        plan = build_plan(run_id=f"C{it}", holdings=holds, free_cash=cash,
                          watchlist=wl, ltp=px,
                          security_ids={s: f"H{s}" for s in syms},
                          cfg=cfg, circuit=circ)
        if plan.blockers:
            continue
        sells = [o for o in plan.orders if o.side is Side.SELL]
        buys = [o for o in plan.orders if o.side is Side.BUY]
        proceeds = (sum(o.value for o in sells)
                    * (1 - cfg["costs"]["est_sell_cost_pct"])
                    - cfg["costs"]["dp_charge_per_scrip_inr"] * len(sells))
        need = sum(o.value for o in buys) * (1 + cfg["costs"]["est_buy_cost_pct"])
        assert need <= cash + proceeds + 1.0, (
            f"iter {it}: Rs.{need:,.0f} chahiye par sirf "
            f"Rs.{cash + proceeds:,.0f} hai")

        # kabhi bhi DP-free qty se zyada mat becho
        avail = {h.symbol: h.available_qty for h in holds}
        for o in sells:
            assert o.qty <= avail.get(o.symbol, 0), f"{o.symbol} oversell"
