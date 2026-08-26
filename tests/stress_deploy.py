"""DEPLOY BUDGET stress -- "poori capital market mein nahi lagani".

Ye file sirf ek sawaal poochti hai, hazaaron tarike se:
    jitna paisa maine lagane ko bola, usse ZYADA toh nahi laga diya?

Chalao:  python tests/stress_deploy.py
"""
import copy, random, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan, resolve_deploy
from stress_weeks import Book                      # nakli demat + bank

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = cfgmod.load(ROOT / "config.yaml")
BASE["risk"]["max_single_order_value_inr"] = 10_00_00_00_000
BASE["risk"]["max_turnover_pct"] = 9.9
BASE["risk"]["min_market_cap_cr"] = 0

FAIL: list[str] = []
CHECKS = 0


def chk(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAIL.append(msg)


def mkcfg(**over):
    c = copy.deepcopy(BASE)
    c["portfolio"].update(over)
    return c


def scenario(cfg, wl_syms, prices, holds, cash, run_id, circ=None):
    wl = [TargetName(rank=i + 1, symbol=s, ref_ltp=prices[s], market_cap_cr=25000.0)
          for i, s in enumerate(wl_syms)]
    sec = {s: f"H{s}" for s in prices}
    return build_plan(run_id=run_id, holdings=holds, free_cash=cash, watchlist=wl,
                      ltp=prices, security_ids=sec, cfg=cfg, circuit=circ or {})


def allowance(plan, cfg, holds, prices, qty):
    """Budget se upar kitna rehna JAAYAZ hai.

    Teen wajah se position target se upar reh sakti hai, aur teenon jaayaz:
      * T+1 unsettled -- wo share aaj bech hi nahi sakte
      * drift band    -- jaan-bujh kar chhoti hilan-dulan ignore karte hain
      * min trade     -- Rs.200 ka trim bhejna charges mein hi chala jaayega
    Uske upar whole-share rounding: har position ek share tak upar.
    """
    stuck = sum(max(0, h.total_qty - h.available_qty) * prices[h.symbol]
                for h in holds)
    band = float(cfg["portfolio"]["drift_band_pct"])
    mt = max(float(cfg["costs"]["min_trade_value_inr"]),
             plan.slice_value * float(cfg["costs"]["min_trade_pct_of_slice"]))
    live = [s for s, v in qty.items() if v > 0]
    rounding = sum(prices[s] for s in live)
    return stuck + rounding + (band * plan.slice_value + mt) * (len(live) + 1)


def equity_after(plan, holds, prices):
    """Plan poora bhar gaya toh stocks mein kitna paisa rahega."""
    q = {h.symbol: h.total_qty for h in holds}
    for o in plan.orders:
        q[o.symbol] = q.get(o.symbol, 0) + (o.qty if o.side is Side.BUY else -o.qty)
    return sum(v * prices[s] for s, v in q.items() if v > 0), q


# ======================================================================
# 1. RESOLVER -- akela, bina kisi portfolio ke
# ======================================================================
def t_resolver():
    nav = 1_00_00_000.0
    cases = [
        ({"deploy_mode": "all"}, nav),
        ({"deploy_mode": "pct", "deploy_pct": 0.6}, nav * 0.6),
        ({"deploy_mode": "pct", "deploy_pct": 60}, nav * 0.6),       # 60 = 60%
        ({"deploy_mode": "pct", "deploy_pct": 1.0}, nav),            # 1.0 = 100%
        ({"deploy_mode": "pct", "deploy_pct": 100}, nav),
        ({"deploy_mode": "pct", "deploy_pct": 0}, 0.0),
        ({"deploy_mode": "pct", "deploy_pct": 250}, nav),            # clamp
        ({"deploy_mode": "pct", "deploy_pct": -5}, 0.0),             # clamp
        ({"deploy_mode": "pct", "deploy_pct": None}, 0.0),
        ({"deploy_mode": "pct", "deploy_pct": "abc"}, nav),          # fallback 1.0
        ({"deploy_mode": "PCT", "deploy_pct": 0.25}, nav * 0.25),    # case
        ({"deploy_mode": " percent ", "deploy_pct": 0.25}, nav * .25),
        ({"deploy_mode": "amount", "deploy_amount": 40_00_000}, 40_00_000),
        ({"deploy_mode": "amount", "deploy_amount": 0}, 0.0),
        ({"deploy_mode": "amount", "deploy_amount": -9}, 0.0),
        ({"deploy_mode": "amount", "deploy_amount": 99_00_00_000}, 99_00_00_000),
        ({"deploy_mode": "amount", "deploy_amount": "x"}, 0.0),
        ({"deploy_mode": "kuchbhi"}, nav),                            # unknown -> all
    ]
    for over, want in cases:
        got, label = resolve_deploy(over, nav)
        chk(abs(got - want) < 0.51, f"resolver {over} -> {got:,.0f}, chahiye {want:,.0f}")
        chk(isinstance(label, str) and label, f"resolver label khaali: {over}")
    # nan
    got, _ = resolve_deploy({"deploy_mode": "pct", "deploy_pct": float("nan")}, nav)
    chk(got == 0.0, f"NaN pct -> {got}")
    got, _ = resolve_deploy({"deploy_mode": "amount", "deploy_amount": float("nan")}, nav)
    chk(got == 0.0, f"NaN amount -> {got}")
    # nav 0
    for m in ("all", "pct", "amount"):
        got, _ = resolve_deploy({"deploy_mode": m, "deploy_pct": .5,
                                 "deploy_amount": 5_00_000}, 0.0)
        chk(got >= 0, f"nav=0 {m} -> {got}")


# ======================================================================
# 2. TIGHT INVARIANT -- sab settled, min_trade 0, band 0
#    Sirf whole-share rounding ki chhoot hai.
# ======================================================================
def t_tight(iters=4000):
    rng = random.Random(20260821)
    worst = 0.0
    for it in range(iters):
        n_wl = rng.randint(1, 22)
        syms = [f"W{i}" for i in range(n_wl)] + [f"X{i}" for i in range(5)]
        prices = {s: round(rng.uniform(15, 3500), 2) for s in syms}
        wl_syms = [f"W{i}" for i in range(n_wl)]

        mode = rng.choice(["all", "pct", "amount"])
        pct = rng.choice([0, 0.01, 0.05, 0.25, 0.4, 0.6, 0.75, 0.99, 1.0])
        amt = rng.choice([0, 5_000, 1_00_000, 10_00_000, 75_00_000,
                          2_00_00_000, 50_00_00_000])
        cfg = mkcfg(n_stocks="auto", exit_rank_threshold="auto",
                    drift_band_pct=0.0,
                    cash_reserve_pct=rng.choice([0.0, 0.01, 0.03]),
                    use_overflow_slot=rng.random() < 0.7,
                    partial_list_mode=rng.choice(["full", "fixed_slots"]),
                    max_weight_per_stock_pct=None,
                    deploy_mode=mode, deploy_pct=pct, deploy_amount=amt)
        cfg["costs"]["min_trade_value_inr"] = 0
        cfg["costs"]["min_trade_pct_of_slice"] = 0.0

        holds = []
        for s in rng.sample(syms, rng.randint(0, min(10, len(syms)))):
            q = rng.randint(1, 3000)
            holds.append(Position(symbol=s, security_id=f"H{s}", total_qty=q,
                                  available_qty=q,                # sab settled
                                  avg_price=prices[s]))
        cash = round(rng.uniform(0, 3_00_00_000), 2)
        p = scenario(cfg, wl_syms, prices, holds, cash, f"T{it}")
        if p.blockers:
            continue

        eq, qty = equity_after(p, holds, prices)
        te = p.target_equity
        # rounding slack: har position ek share tak upar ja sakti hai
        slack = allowance(p, cfg, holds, prices, qty) + 1.0
        if eq > te + slack:
            FAIL.append(f"TIGHT #{it}: equity {eq:,.0f} > target {te:,.0f} "
                        f"(+slack {slack:,.0f}) mode={mode} pct={pct} amt={amt}")
        worst = max(worst, (eq - te) / max(slack, 1.0))

        for s, v in qty.items():
            chk(v >= 0, f"TIGHT #{it}: {s} qty {v} negative")
        b = {o.symbol for o in p.orders if o.side is Side.BUY}
        s_ = {o.symbol for o in p.orders if o.side is Side.SELL}
        chk(not (b & s_), f"TIGHT #{it}: wash trade {sorted(b & s_)}")
    return worst


# ======================================================================
# 3. BROAD FUZZ -- sab kuch random (unsettled, band, weight cap, min_trade)
#    Yahan sirf ye dekha jaata hai ki planner ne exposure BADHAYA toh nahi.
# ======================================================================
def t_broad(iters=6000):
    rng = random.Random(4242)
    for it in range(iters):
        n_wl = rng.randint(1, 30)
        syms = [f"W{i}" for i in range(n_wl)] + [f"X{i}" for i in range(8)]
        prices = {s: round(rng.uniform(1.5, 60000), 2) for s in syms}
        wl_syms = [f"W{i}" for i in range(n_wl)]
        mode = rng.choice(["all", "pct", "amount"])
        cfg = mkcfg(
            n_stocks=rng.choice(["auto", rng.randint(1, 20)]),
            exit_rank_threshold="auto",
            drift_band_pct=rng.choice([0.0, 0.0, 0.05, 0.25]),
            cash_reserve_pct=rng.choice([0.0, 0.01, 0.05, 0.2]),
            use_overflow_slot=rng.random() < 0.7,
            partial_list_mode=rng.choice(["full", "fixed_slots"]),
            max_weight_per_stock_pct=rng.choice([None, None, 0.25, 0.1]),
            deploy_mode=mode,
            deploy_pct=rng.choice([0, 0.03, 0.5, 0.9, 1.0, 60, 100]),
            deploy_amount=rng.choice([0, 1, 999, 5_00_000, 1_00_00_000, 9_00_00_00_000]))
        holds = []
        for s in rng.sample(syms, rng.randint(0, min(12, len(syms)))):
            q = rng.randint(1, 5000)
            a = q if rng.random() < 0.7 else rng.randint(0, q)
            holds.append(Position(symbol=s, security_id=f"H{s}", total_qty=q,
                                  available_qty=a, avg_price=prices[s] * rng.uniform(.5, 1.6)))
        cash = round(rng.uniform(0, 5_00_00_000), 2)
        circ = {s: CircuitInfo(symbol=s, ltp=prices[s], upper=prices[s] * 1.2,
                               lower=prices[s] * .8, prev_close=prices[s],
                               volume=10_00_000) for s in syms}
        try:
            p = scenario(cfg, wl_syms, prices, holds, cash, f"B{it}", circ)
        except Exception as e:
            FAIL.append(f"BROAD #{it}: CRASH {type(e).__name__}: {e}")
            continue
        if p.blockers:
            continue

        eq_before = sum(h.total_qty * prices[h.symbol] for h in holds)
        eq, qty = equity_after(p, holds, prices)
        te = p.target_equity
        ceiling = max(eq_before, te) + allowance(p, cfg, holds, prices, qty)
        chk(eq <= ceiling,
            f"BROAD #{it}: equity {eq:,.0f} > ceiling {ceiling:,.0f} "
            f"(before {eq_before:,.0f}, target {te:,.0f}, mode={mode})")

        # cash conservation
        buys = [o for o in p.orders if o.side is Side.BUY]
        sells = [o for o in p.orders if o.side is Side.SELL]
        c = cfg["costs"]
        proceeds = (sum(o.value for o in sells) * (1 - float(c["est_sell_cost_pct"]))
                    - float(c["dp_charge_per_scrip_inr"]) * len(sells))
        need = sum(o.value for o in buys) * (1 + float(c["est_buy_cost_pct"]))
        chk(need <= cash + proceeds + 1.0,
            f"BROAD #{it}: cash short by {need - cash - proceeds:,.0f}")

        held_map = {h.symbol: h for h in holds}
        for o in sells:
            h = held_map.get(o.symbol)
            chk(h is not None and o.qty <= h.available_qty,
                f"BROAD #{it}: {o.symbol} SELL {o.qty} > sellable "
                f"{h.available_qty if h else 0}")
        for o in p.orders:
            chk(o.qty > 0, f"BROAD #{it}: {o.symbol} qty {o.qty}")
            chk(o.limit_price is None or o.limit_price > 0,
                f"BROAD #{it}: {o.symbol} limit {o.limit_price}")
        b = {o.symbol for o in buys}
        s_ = {o.symbol for o in sells}
        chk(not (b & s_), f"BROAD #{it}: wash {sorted(b & s_)}")
        chk(p.target_equity >= 0 and p.slice_value >= 0,
            f"BROAD #{it}: negative target/slice")


# ======================================================================
# 4. CONVERGENCE -- budget ghatao, plan chalao, dobara plan banao.
#    Do round ke baad koi order nahi bachna chahiye (flip-flop nahi).
# ======================================================================
def t_converge():
    rng = random.Random(9)
    for trial in range(120):
        n = rng.randint(3, 14)
        syms = [f"W{i}" for i in range(n)]
        prices = {s: round(rng.uniform(50, 1800), 2) for s in syms}
        cfg = mkcfg(n_stocks="auto", exit_rank_threshold="auto",
                    drift_band_pct=0.0, cash_reserve_pct=0.01,
                    use_overflow_slot=rng.random() < .7,
                    partial_list_mode="full", max_weight_per_stock_pct=None,
                    deploy_mode="all")
        book = Book(rng.choice([5_00_000, 50_00_000, 5_00_00_000]))
        # hafta 1: poora deploy
        for round_no in range(2):
            p = scenario(cfg, syms, prices, book.positions(), book.cash,
                         f"C{trial}-{round_no}")
            if p.blockers:
                break
            for o in p.orders:
                book.apply(o, o.qty, o.ref_price, cfg)
            book.settle_day()

        # ab budget kaato
        pct = rng.choice([0.0, 0.1, 0.35, 0.5, 0.8])
        cfg["portfolio"].update(deploy_mode="pct", deploy_pct=pct)
        nav0 = book.value(prices) + book.cash
        target = nav0 * pct

        last_orders = None
        for round_no in range(4):
            p = scenario(cfg, syms, prices, book.positions(), book.cash,
                         f"D{trial}-{round_no}")
            if p.blockers:
                FAIL.append(f"CONVERGE #{trial} r{round_no} BLOCKED: {p.blockers[:1]}")
                break
            last_orders = len(p.orders)
            if not p.orders:
                break
            for o in p.orders:
                book.apply(o, o.qty, o.ref_price, cfg)
            book.settle_day()
        else:
            if last_orders:
                FAIL.append(f"CONVERGE #{trial}: 4 round baad bhi {last_orders} "
                            f"order bache (pct={pct}) -- flip-flop?")

        eq = book.value(prices)
        qmap = {k: v[0] for k, v in book.pos.items()}
        slack = allowance(p, cfg, book.positions(), prices, qmap)
        chk(eq <= target + slack,
            f"CONVERGE #{trial}: pct={pct} equity {eq:,.0f} > target {target:,.0f} "
            f"(+slack {slack:,.0f})")
        chk(book.cash >= -1.0, f"CONVERGE #{trial}: cash {book.cash:,.0f} negative")


# ======================================================================
# 5. MULTI-WEEK -- budget har hafte badalta hai, list bhi badalti hai
# ======================================================================
def t_weeks():
    rng = random.Random(31337)
    UNI = [f"S{i:03d}" for i in range(40)]
    for trial in range(40):
        px = {s: round(rng.uniform(30, 2200), 2) for s in UNI}
        cfg = mkcfg(n_stocks="auto", exit_rank_threshold="auto",
                    drift_band_pct=rng.choice([0.0, 0.1]),
                    cash_reserve_pct=0.01,
                    use_overflow_slot=rng.random() < .7,
                    partial_list_mode="full", max_weight_per_stock_pct=None,
                    deploy_mode="all")
        book = Book(rng.choice([10_00_000, 1_00_00_000]))
        for wk in range(26):
            for s in UNI:
                px[s] = round(max(1.0, px[s] * (1 + rng.gauss(0, 0.05))), 2)
            names = rng.sample(UNI, rng.randint(2, 15))
            # har hafte budget badal do -- sabse kharab case
            r = rng.random()
            if r < .3:
                cfg["portfolio"].update(deploy_mode="all")
            elif r < .75:
                cfg["portfolio"].update(deploy_mode="pct",
                                        deploy_pct=rng.choice([0, .2, .5, .7, .95, 1.0]))
            else:
                cfg["portfolio"].update(deploy_mode="amount",
                                        deploy_amount=rng.choice(
                                            [0, 2_00_000, 25_00_000, 5_00_00_000]))
            holds = book.positions()
            eq_before = book.value(px)
            try:
                p = scenario(cfg, names, px, holds, book.cash, f"W{trial}-{wk}")
            except Exception as e:
                FAIL.append(f"WEEKS #{trial} wk{wk}: CRASH {type(e).__name__}: {e}")
                break
            if p.blockers:
                continue
            for o in p.orders:
                book.apply(o, o.qty, o.ref_price, cfg)
            eq_after = book.value(px)
            qmap = {k: v[0] for k, v in book.pos.items()}
            ceiling = (max(eq_before, p.target_equity)
                       + allowance(p, cfg, holds, px, qmap))
            chk(eq_after <= ceiling,
                f"WEEKS #{trial} wk{wk}: equity {eq_after:,.0f} > ceiling {ceiling:,.0f}")
            chk(book.cash >= -1.0,
                f"WEEKS #{trial} wk{wk}: cash {book.cash:,.0f} negative")
            book.settle_day()


# ======================================================================
# 6. HAATH SE LIKHE HUE EDGE CASES -- naam ke saath, taaki fail ho toh pata chale
# ======================================================================
def t_named():
    px = {"A": 100.0, "B": 200.0, "C": 50.0, "D": 1000.0}
    sec = {s: f"H{s}" for s in px}
    wl = [TargetName(rank=i + 1, symbol=s, ref_ltp=px[s], market_cap_cr=25000.0)
          for i, s in enumerate(["A", "B", "C"])]

    def plan(cfg, holds, cash, rid):
        return build_plan(run_id=rid, holdings=holds, free_cash=cash, watchlist=wl,
                          ltp=px, security_ids=sec, cfg=cfg, circuit={})

    base = dict(n_stocks="auto", exit_rank_threshold="auto", drift_band_pct=0.0,
                cash_reserve_pct=0.0, use_overflow_slot=False,
                partial_list_mode="full", max_weight_per_stock_pct=None)

    # -- 0% deploy, portfolio bhara hua -> sab nikalna chahiye ------------
    c = mkcfg(**base, deploy_mode="pct", deploy_pct=0.0)
    c["risk"]["max_turnover_pct"] = 9.9
    holds = [Position("A", "HA", 100, 100, 100.0), Position("B", "HB", 50, 50, 200.0)]
    p = plan(c, holds, 0.0, "N1")
    eq, q = equity_after(p, holds, px)
    chk(not p.blockers, f"N1 blocked: {p.blockers}")
    chk(eq == 0, f"N1: deploy 0% par bhi {eq:,.0f} stocks mein raha")
    chk(all(o.side is Side.SELL for o in p.orders), "N1: 0% deploy par BUY order aaya")
    chk(p.target_equity == 0, f"N1: target_equity {p.target_equity}")

    # -- 0% deploy churn gate -> blocker mein deploy ka zikr ho ------------
    c2 = mkcfg(**base, deploy_mode="pct", deploy_pct=0.0)
    c2["risk"]["max_turnover_pct"] = 0.5
    p = plan(c2, holds, 0.0, "N2")
    chk(any("deploy budget" in b.lower() for b in p.blockers),
        f"N2: churn blocker mein deploy ka zikr nahi: {p.blockers}")

    # -- deploy amount > NAV -> cap + warning -----------------------------
    c = mkcfg(**base, deploy_mode="amount", deploy_amount=10_00_00_000)
    p = plan(c, [], 5_00_000.0, "N3")
    chk(abs(p.target_equity - 5_00_000) < 1,
        f"N3: target {p.target_equity:,.0f}, NAV 5,00,000 tha")
    chk(any("se bada hai" in w for w in p.warnings), f"N3: warning nahi aayi")

    # -- deploy amount chhota -> ek share bhi nahi ------------------------
    c = mkcfg(**base, deploy_mode="amount", deploy_amount=10.0)
    p = plan(c, [], 5_00_000.0, "N4")
    chk(not p.blockers, f"N4 blocked: {p.blockers}")
    chk(not [o for o in p.orders if o.side is Side.BUY],
        f"N4: Rs.10 budget mein bhi BUY aaya: {[(o.symbol,o.qty) for o in p.orders]}")

    # -- 50% deploy, khaali portfolio -> aadha lage ------------------------
    c = mkcfg(**base, deploy_mode="pct", deploy_pct=0.5)
    p = plan(c, [], 12_00_000.0, "N5")
    eq, _ = equity_after(p, [], px)
    chk(abs(p.target_equity - 6_00_000) < 1, f"N5: target {p.target_equity}")
    chk(eq <= 6_00_000 + 1, f"N5: {eq:,.0f} laga, 6,00,000 chahiye tha")
    chk(eq > 5_50_000, f"N5: sirf {eq:,.0f} laga -- kam laga diya")

    # -- OVERFLOW SLOT bhi budget ke andar rahe (ye purana bada bug tha) ---
    c = mkcfg(**{**base, "use_overflow_slot": True}, deploy_mode="pct", deploy_pct=0.5)
    p = plan(c, [], 12_00_000.0, "N6")
    eq, _ = equity_after(p, [], px)
    chk(eq <= 6_00_000 + max(px.values()),
        f"N6: n+1 slot ne budget tod diya -- {eq:,.0f} laga, 6,00,000 cap tha")

    # -- cash reserve bhi n+1 se na tootey --------------------------------
    c = mkcfg(**{**base, "use_overflow_slot": True, "cash_reserve_pct": 0.10},
              deploy_mode="all")
    p = plan(c, [], 10_00_000.0, "N7")
    eq, _ = equity_after(p, [], px)
    chk(eq <= 9_00_000 + max(px.values()),
        f"N7: 10% reserve n+1 slot kha gaya -- {eq:,.0f} laga, 9,00,000 cap tha")

    # -- budget < holdings -> n+1 bhi trim ho -----------------------------
    c = mkcfg(**{**base, "use_overflow_slot": True}, deploy_mode="amount",
              deploy_amount=1_00_000)
    c["risk"]["max_turnover_pct"] = 9.9
    holds = [Position("A", "HA", 200, 200, 100.0),   # 20,000
             Position("B", "HB", 100, 100, 200.0),   # 20,000
             Position("C", "HC", 2000, 2000, 50.0)]  # 1,00,000  <- n+1
    p = plan(c, holds, 0.0, "N8")
    eq, _ = equity_after(p, holds, px)
    chk(eq <= 1_00_000 + max(px.values()),
        f"N8: budget 1,00,000 tha par {eq:,.0f} raha")

    # -- unsettled qty: bech nahi sakte -> skip, crash nahi ----------------
    c = mkcfg(**base, deploy_mode="pct", deploy_pct=0.0)
    c["risk"]["max_turnover_pct"] = 9.9
    holds = [Position("A", "HA", 100, 0, 100.0)]      # sellable 0
    p = plan(c, holds, 0.0, "N9")
    chk(not [o for o in p.orders if o.side is Side.SELL and o.symbol == "A"],
        "N9: unsettled share bech diya")

    # -- NAV 0 -> saaf blocker, crash nahi ---------------------------------
    c = mkcfg(**base, deploy_mode="pct", deploy_pct=0.5)
    p = plan(c, [], 0.0, "N10")
    chk(p.blockers, "N10: NAV 0 par blocker nahi aaya")

    # -- deploy 100% == purana behaviour (regression) ----------------------
    c_old = mkcfg(**base, deploy_mode="all")
    c_new = mkcfg(**base, deploy_mode="pct", deploy_pct=1.0)
    h = [Position("A", "HA", 30, 30, 90.0)]
    p1 = plan(c_old, h, 4_00_000.0, "N11a")
    p2 = plan(c_new, h, 4_00_000.0, "N11b")
    o1 = sorted((o.symbol, o.side.value, o.qty) for o in p1.orders)
    o2 = sorted((o.symbol, o.side.value, o.qty) for o in p2.orders)
    chk(o1 == o2, f"N11: 100% deploy purane behaviour se alag hai\n{o1}\n{o2}")

    # -- amount == NAV exactly ---------------------------------------------
    c = mkcfg(**base, deploy_mode="amount", deploy_amount=4_00_000)
    p = plan(c, [], 4_00_000.0, "N12")
    chk(not p.blockers and not any("se bada hai" in w for w in p.warnings),
        f"N12: exact NAV par galat warning: {p.warnings}")

    # -- keep-zone parked bhi budget mein gine jaayein ---------------------
    c = mkcfg(**{**base, "n_stocks": 2, "exit_rank_threshold": 3},
              deploy_mode="amount", deploy_amount=1_00_000)
    c["risk"]["max_turnover_pct"] = 9.9
    holds = [Position("C", "HC", 1000, 1000, 50.0)]       # 50,000 parked (rank 3)
    p = plan(c, holds, 2_00_000.0, "N13")
    eq, _ = equity_after(p, holds, px)
    chk(eq <= 1_00_000 + max(px.values()),
        f"N13: parked ko ginti mein nahi liya -- {eq:,.0f} vs cap 1,00,000")


# ======================================================================
if __name__ == "__main__":
    print("DEPLOY BUDGET STRESS")
    print("-" * 62)
    t_resolver();  print(f"  1. resolver              {CHECKS:5d} checks")
    n = CHECKS; worst = t_tight()
    print(f"  2. tight invariant       {CHECKS-n:5d} checks  "
          f"(rounding slack ka max {worst*100:.1f}% use hua)")
    n = CHECKS; t_broad()
    print(f"  3. broad fuzz            {CHECKS-n:5d} checks")
    n = CHECKS; t_converge()
    print(f"  4. convergence           {CHECKS-n:5d} checks")
    n = CHECKS; t_weeks()
    print(f"  5. 26-week sequential    {CHECKS-n:5d} checks")
    n = CHECKS; t_named()
    print(f"  6. named edge cases      {CHECKS-n:5d} checks")
    print("-" * 62)
    if FAIL:
        print(f"  {len(FAIL)} FAIL (pehle 25):")
        for f in FAIL[:25]:
            print("   x", f)
        sys.exit(1)
    print(f"  SAB PASS -- {CHECKS:,} checks, zero fail.")
