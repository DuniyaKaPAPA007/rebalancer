"""
T+1 Settlement & Monthly EOM Backtest + Regression Suite
Har known/unknown scenario jo monthly rebalance me aa sakta hai.
"""
import copy, random, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan, build_liquidation_plan
from rebalancer.executor import Executor
from rebalancer.store import Store
import rebalancer.config as cfgmod

def base_cfg():
    cfg = cfgmod.load(ROOT / "config.yaml")
    # ensure T1 defaults
    cfg["execution"].setdefault("settlement_T1", True)
    cfg["execution"].setdefault("rebalance_schedule", "monthly_eom")
    cfg["risk"]["max_single_order_value_inr"] = 10_00_00_000
    cfg["risk"]["max_turnover_pct"] = 9.9
    return cfg

def wl(symbols):
    return [TargetName(rank=i+1, symbol=s, ref_ltp=100) for i,s in enumerate(symbols)]

# ---- 1. T1 separates SELL and BUY into 2 phases ----
def test_t1_sell_buy_separated():
    cfg = base_cfg()
    cfg["execution"]["settlement_T1"] = True
    wl_ = wl([f"S{i}" for i in range(10)])
    ltp = {t.symbol: 100 for t in wl_}
    sec = {t.symbol: f"ID{t.symbol}" for t in wl_}
    holdings = [Position("OLD1","ID_OLD1",1000,1000, avg_price=100), Position("OLD2","ID_OLD2",1000,1000)]
    ltp.update({"OLD1":100,"OLD2":100})
    sec.update({"OLD1":"ID_OLD1","OLD2":"ID_OLD2"})
    plan = build_plan(run_id="T1", holdings=holdings, free_cash=50000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    # T1: sells exist, buys limited to free_cash only
    assert any("T+1" in w for w in plan.warnings)
    buys = [o for o in plan.orders if o.side==Side.BUY]
    # buys value should be <= free_cash + small, not free_cash+sell_proceeds
    assert sum(o.value for o in buys) <= 50000*1.01 + 14.75*2  # allow DP

def test_t1_executor_splits_phases():
    cfg = base_cfg()
    cfg["execution"]["settlement_T1"] = True
    wl_ = wl([f"S{i}" for i in range(5)])
    ltp = {t.symbol:100 for t in wl_}
    sec = {t.symbol:f"ID{t.symbol}" for t in wl_}
    holdings=[Position("OLD","ID_OLD",500,500)]
    ltp["OLD"]=100; sec["OLD"]="ID_OLD"
    plan = build_plan(run_id="E1", holdings=holdings, free_cash=20000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    # force some sells + buys
    assert plan.sells and plan.buys
    # LIVE executor should defer buys
    class Fake:
        def available_cash(self): return 20000
        def holdings(self): return holdings
        def ltp(self, ids): return {i:100 for i in ids}
        def place_order(self, **kw): return {"orderId":"OID1","orderStatus":"TRADED"}
        def order(self, oid): return {"orderStatus":"TRADED","filledQty":1}
    import tempfile
    db = Store(Path(tempfile.gettempdir())/ "t1_test.db")
    db.save_run(plan.run_id, "2026-01-31T10:00:00", "PLANNED", plan.nav, plan.free_cash, plan.slice_value, "{}")
    # dry_run should still show both sells and buys (for preview)
    r_dry = Executor(Fake(), db, cfg, dry_run=True).run(plan)
    assert len(r_dry["sells"]) >0 and len(r_dry["buys"])>0
    # live should defer buys
    r_live = Executor(Fake(), db, cfg, dry_run=False).run(plan)
    assert len(r_live["sells"])>0
    assert r_live.get("t1_pending_buys") == len(plan.buys)
    assert r_live["buys"] == []  # skipped in T1 live

def test_t1_no_sell_then_buy_same_day():
    # if no sells, buys should execute same day even in T1 (no settlement needed)
    cfg = base_cfg()
    cfg["execution"]["settlement_T1"] = True
    wl_ = wl([f"S{i}" for i in range(5)])
    ltp = {t.symbol:100 for t in wl_}
    sec = {t.symbol:f"ID{t.symbol}" for t in wl_}
    # no holdings at all, so no sells, only buys from free cash
    plan = build_plan(run_id="E2", holdings=[], free_cash=50000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    assert not plan.sells
    assert plan.buys
    # executor live should still do buys (no sells to wait for)
    class Fake2:
        def available_cash(self): return 50000
        def holdings(self): return []
        def ltp(self, ids): return {i:100 for i in ids}
        def place_order(self, **kw): return {"orderId":"OID","orderStatus":"TRADED"}
        def order(self, oid): return {"orderStatus":"TRADED","filledQty":10}
    import tempfile
    from pathlib import Path
    db = Store(Path(tempfile.gettempdir())/ "t1_test2.db")
    db.save_run(plan.run_id, "2026-01-15T10:00:00", "PLANNED", plan.nav, plan.free_cash, plan.slice_value, "{}")
    r = Executor(Fake2(), db, cfg, dry_run=False).run(plan)
    assert len(r["buys"])>0

# ---- 2. Monthly 12-month walk-forward backtest ----
def test_12_month_walk_forward_no_crash():
    cfg = base_cfg()
    cfg["execution"]["settlement_T1"] = True
    cfg["portfolio"]["n_stocks"] = 10
    random.seed(42)
    # simulate 12 months, each month watchlist changes randomly 30%
    base_syms = [f"STK{i:02d}" for i in range(30)]
    holdings = []
    cash = 1000000.0
    navs = []
    for month in range(12):
        # each month's top 10 randomly chosen to simulate momentum churn
        wl_syms = random.sample(base_syms, 10)
        # add overflow 11th
        wl_syms_11 = wl_syms + [random.choice([s for s in base_syms if s not in wl_syms])]
        wl_ = [TargetName(rank=i+1, symbol=s, ref_ltp=100+random.uniform(-20,20)) for i,s in enumerate(wl_syms_11)]
        # random price moves +/-10% each month
        ltp = {t.symbol: max(10, t.ref_ltp * random.uniform(0.9,1.1)) for t in wl_}
        # include holdings symbols in ltp/sec
        for h in holdings:
            if h.symbol not in ltp:
                ltp[h.symbol] = 100 * random.uniform(0.9,1.1)
        sec = {s: f"ID_{s}" for s in ltp}
        # add circuit random for some
        circuit = {}
        for sym, px in ltp.items():
            if random.random()<0.1:
                # narrow band
                circuit[sym]=CircuitInfo(sym, ltp=px, upper=px*1.02, lower=px*0.98, prev_close=px, volume=5000)
            else:
                circuit[sym]=CircuitInfo(sym, ltp=px, upper=px*1.2, lower=px*0.8, prev_close=px, volume=10_000_00)
        plan = build_plan(run_id=f"M{month:02d}", holdings=holdings, free_cash=cash, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit=circuit)
        assert not plan.blockers or "khaali" not in str(plan.blockers).lower()  # allow blockers for turnover etc but not crash
        if plan.blockers:
            # if blocked, skip execution
            continue
        # Simulate T+1 execution: sell first, then cash increases by sell proceeds next month
        sell_val = sum(o.value for o in plan.orders if o.side==Side.SELL)
        buy_val = sum(o.value for o in plan.orders if o.side==Side.BUY)
        # In T1, buys limited to cash, so buy_val should <= cash (approx)
        if sell_val>0:
            assert buy_val <= cash*1.02 + 1, f"month {month} T1 buy {buy_val} > cash {cash}"
        # simulate fills: update holdings
        # remove sells
        sell_qty = {o.symbol: o.qty for o in plan.orders if o.side==Side.SELL}
        new_hold={}
        for h in holdings:
            q = h.total_qty - sell_qty.get(h.symbol,0)
            if q>0:
                new_hold[h.symbol]=Position(h.symbol, h.security_id, q, q, avg_price=ltp.get(h.symbol,100))
        # add buys
        for o in plan.orders:
            if o.side==Side.BUY:
                if o.symbol in new_hold:
                    cur = new_hold[o.symbol]
                    new_hold[o.symbol]=Position(o.symbol, o.security_id, cur.total_qty+o.qty, cur.total_qty+o.qty, avg_price=ltp[o.symbol])
                else:
                    new_hold[o.symbol]=Position(o.symbol, o.security_id, o.qty, o.qty, avg_price=ltp[o.symbol])
        holdings = list(new_hold.values())
        # cash update: for T1, cash after sells = cash - buys + sells (but sells proceeds arrive next month, so for next month's cash)
        # Simulate: month end cash after sells is cash - buys (buys from cash only) , sells proceeds added to next month free_cash
        # For backtest we model: cash_next = cash - buy_val + sell_val*0.998 (costs)
        # But in our simplified, T1 planner already limited buys to cash, so next month free_cash = cash - buy_val + sell_val
        cash = cash - buy_val + sell_val*0.998  # approximate costs
        # add small random dividend/interest
        cash = max(0, cash)
        nav = sum(h.total_qty * ltp.get(h.symbol,100) for h in holdings) + cash
        navs.append(nav)
        # check no wash trade
        for sym in ltp:
            sides = {o.side for o in plan.orders if o.symbol==sym}
            assert not (Side.BUY in sides and Side.SELL in sides), f"wash {sym} month {month}"
    # nav should not go to zero or explode
    assert len(navs) >= 8  # at least 8 months not blocked
    assert all(n>100000 for n in navs), "NAV collapsed"
    # check final holdings count approx n
    # not strict but should be around n+some overflow
    assert 1 <= len(holdings) <= 15

# ---- 3. Edge scenarios for T1 ----
def test_t1_with_zero_cash_and_large_sells():
    cfg = base_cfg()
    cfg["execution"]["settlement_T1"]=True
    wl_ = wl([f"S{i}" for i in range(5)])
    ltp={t.symbol:100 for t in wl_}
    sec={t.symbol:f"ID{t.symbol}" for t in wl_}
    holdings=[Position("OLD","ID_OLD",10000,10000)]  # 10L value
    ltp["OLD"]=100; sec["OLD"]="ID_OLD"
    plan=build_plan(run_id="Z", holdings=holdings, free_cash=0, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    # T1 with 0 cash: buys should be 0 (no cash), sells should still happen
    assert plan.sells
    assert len([o for o in plan.orders if o.side==Side.BUY])==0
    assert any("T+1" in w for w in plan.warnings)

def test_t1_partial_list_and_overflow():
    cfg=base_cfg()
    cfg["execution"]["settlement_T1"]=True
    cfg["portfolio"]["n_stocks"]=2
    cfg["portfolio"]["exit_rank_threshold"]=2
    cfg["portfolio"]["partial_list_mode"]="full"
    # only 2 stocks in list, n=2 -> full mode => 2 buys should still respect T1
    wl_=[TargetName(rank=1, symbol="A", ref_ltp=100), TargetName(rank=2, symbol="B", ref_ltp=200)]
    ltp={"A":100,"B":200}
    sec={"A":"IDA","B":"IDB"}
    plan=build_plan(run_id="P", holdings=[], free_cash=10000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    assert not plan.blockers
    # should allocate ~4950 each (1% reserve)
    assert len([o for o in plan.orders if o.side==Side.BUY])==2

def test_t1_with_circuit_skip():
    cfg=base_cfg()
    cfg["execution"]["settlement_T1"]=True
    wl_=wl(["A","B"])
    ltp={"A":100,"B":100}
    sec={"A":"IDA","B":"IDB"}
    # A at upper circuit, BUY should be clamped not rejected, but warning should show
    circuit={"A": CircuitInfo("A", ltp=100, upper=100, lower=80, prev_close=90, volume=10000),
             "B": CircuitInfo("B", ltp=100, upper=120, lower=80, prev_close=100, volume=10000000)}
    plan=build_plan(run_id="C", holdings=[], free_cash=20000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit=circuit)
    assert any("UPPER CIRCUIT" in w for w in plan.warnings)

def test_t1_vs_same_day_toggle_consistency():
    # same inputs, T1 True should have <= buys value than T1 False (since budget smaller)
    cfg1=base_cfg(); cfg1["execution"]["settlement_T1"]=False
    cfg2=base_cfg(); cfg2["execution"]["settlement_T1"]=True
    wl_=wl([f"S{i}" for i in range(5)])
    ltp={t.symbol:100 for t in wl_}
    sec={t.symbol:f"ID{t.symbol}" for t in wl_}
    holdings=[Position("OLD","ID_OLD",1000,1000)]
    ltp["OLD"]=100; sec["OLD"]="ID_OLD"
    p1=build_plan(run_id="A", holdings=holdings, free_cash=10000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg1, circuit={})
    p2=build_plan(run_id="A", holdings=holdings, free_cash=10000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg2, circuit={})
    v1=sum(o.value for o in p1.orders if o.side==Side.BUY)
    v2=sum(o.value for o in p2.orders if o.side==Side.BUY)
    assert v2 <= v1  # T1 more constrained

def test_known_bug_no_wash_after_t1():
    cfg=base_cfg()
    cfg["execution"]["settlement_T1"]=True
    # previous overflow wash bug should not reappear even with T1
    import copy as cp
    cfg["portfolio"]["n_stocks"]=5
    cfg["portfolio"]["use_overflow_slot"]=True
    wl_=[TargetName(rank=i+1, symbol=s, ref_ltp=950) for i,s in enumerate([f"T{i}" for i in range(5)]+["OV"])]
    sec={t.symbol:f"ID{t.symbol}" for t in wl_}
    ltp={t.symbol:950 for t in wl_}
    sec["OV"]="IDOV"
    holdings=[Position("OV","IDOV",500,500, avg_price=900)]
    plan=build_plan(run_id="W", holdings=holdings, free_cash=9000000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={s:CircuitInfo(s, ltp=950, upper=1140, lower=760, prev_close=950, volume=1000000) for s in ltp})
    for sym in ltp:
        sides={o.side for o in plan.orders if o.symbol==sym}
        assert not (Side.BUY in sides and Side.SELL in sides), f"wash {sym}"

def test_empty_watchlist_blocked_with_t1():
    cfg=base_cfg()
    cfg["execution"]["settlement_T1"]=True
    plan=build_plan(run_id="E", holdings=[], free_cash=10000, watchlist=[], ltp={}, security_ids={}, cfg=cfg, circuit={})
    assert plan.blockers

def test_deploy_pct_with_t1():
    cfg=base_cfg()
    cfg["execution"]["settlement_T1"]=True
    cfg["portfolio"]["deploy_mode"]="pct"
    cfg["portfolio"]["deploy_pct"]=50  # 50%
    wl_=wl([f"S{i}" for i in range(4)])
    ltp={t.symbol:100 for t in wl_}
    sec={t.symbol:f"ID{t.symbol}" for t in wl_}
    plan=build_plan(run_id="D", holdings=[], free_cash=100000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg, circuit={})
    # target equity 50% => 50000, buys should be ~50000, not 100000
    buy_val=sum(o.value for o in plan.orders if o.side==Side.BUY)
    assert 49000 < buy_val < 51000

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
