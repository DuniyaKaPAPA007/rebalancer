"""
Hardening tests - every bug found in audit gets a regression test.
"""
import math
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rebalancer.models import Position, TargetName, CircuitInfo, PlannedOrder, Side, Reason
from rebalancer.planner import build_plan, resolve_n, resolve_exit_rank, resolve_deploy
import rebalancer.config as cfgmod

# base config used across tests
CFG_BASE = {
    "dhan": {"client_id_env": "DHAN_CLIENT_ID", "access_token_env": "DHAN_ACCESS_TOKEN",
             "base_url": "https://api.dhan.co/v2", "exchange_segment": "NSE_EQ", "product_type": "CNC"},
    "portfolio": {"n_stocks": 10, "exit_rank_threshold": 10,
                  "drift_band_pct": 0.0, "cash_reserve_pct": 0.0,
                  "use_overflow_slot": True, "partial_list_mode": "full",
                  "deploy_mode": "all", "deploy_pct": 1.0, "deploy_amount": 0,
                  "max_weight_per_stock_pct": None, "rank_by": "file_order"},
    "costs": {"min_trade_value_inr": 500, "min_trade_pct_of_slice": 0.03,
              "est_sell_cost_pct": 0.0012, "est_buy_cost_pct": 0.0004,
              "dp_charge_per_scrip_inr": 14.75, "rates": {}},
    "execution": {"limit_buffer_pct": 0.003, "order_type": "LIMIT",
                  "market_fallback_after_sec": 300, "fill_poll_interval_sec": 5,
                  "fill_wait_timeout_sec": 420, "phase_gap_sec": 20,
                  "max_plan_age_min": 15},
    "risk": {"max_turnover_pct": 2.0, "max_single_order_value_inr": 10_000_000,
             "min_price_inr": 10, "allowed_window": ["09:45","15:00"],
             "min_market_cap_cr": 0, "stale_ltp_tolerance": 0.05,
             "narrow_band_warn_pct": 10.0, "max_pct_of_traded_value": 0.05},
    "paths": {"watchlist": "watchlist.csv", "db": "runs.db", "plans_dir": "plans", "instruments_cache": ".cache/scrip_master.csv"},
    "prices": {"fallback": ["yahoo","nse"], "stale_warn_min": 20},
}

def make_cfg(overrides=None):
    import copy
    c = copy.deepcopy(CFG_BASE)
    if overrides:
        for sec, vals in overrides.items():
            c[sec].update(vals)
    return c

def wl(symbols):
    return [TargetName(rank=i+1, symbol=s) for i,s in enumerate(symbols)]

def test_models_correlation_no_collision():
    # long symbols with same prefix should not collide after fix
    o1 = PlannedOrder(symbol="RELIANCE", security_id="1", side=Side.BUY, qty=10, ref_price=100, reason=Reason.ENTRY)
    o2 = PlannedOrder(symbol="RELIANCEPP", security_id="2", side=Side.BUY, qty=10, ref_price=100, reason=Reason.ENTRY)
    c1 = o1.correlation_id("R20260826-123456")
    c2 = o2.correlation_id("R20260826-123456")
    assert c1 != c2
    assert len(c1) <= 30 and len(c2) <= 30

def test_models_band_prev_close_zero():
    ci = CircuitInfo(symbol="X", ltp=100, upper=110, lower=90, prev_close=0)
    assert ci.band_pct is None
    ci2 = CircuitInfo(symbol="Y", ltp=100, upper=110, lower=90, prev_close=100)
    # should be ~10% (upper-base)/base
    assert abs(ci2.band_pct - 10.0) < 0.01

def test_models_sellable_caps():
    p = Position("A", "1", total_qty=100, available_qty=150)
    assert p.sellable == 100
    assert p.has_data_issue is True
    p2 = Position("B", "1", total_qty=100, available_qty=-5)
    assert p2.sellable == 0

def test_models_age_invalid():
    from rebalancer.models import Plan
    pl = Plan(run_id="T", nav=0, free_cash=0, slice_value=0, created_ts=0)
    assert pl.age_sec > 100000

def test_planner_duplicate_blocker_returns():
    cfg = make_cfg()
    # duplicate symbol in wl should block and return empty orders
    dup_wl = [TargetName(rank=1, symbol="A"), TargetName(rank=2, symbol="A")]
    plan = build_plan(run_id="T", holdings=[], free_cash=10000, watchlist=dup_wl,
                      ltp={"A":100}, security_ids={"A":"1"}, cfg=cfg)
    assert any("duplicate" in b.lower() for b in plan.blockers)
    assert not plan.orders

def test_planner_holdings_duplicate_aggregated():
    cfg = make_cfg()
    holdings = [Position("A","1",100,100, avg_price=10), Position("A","1",50,50, avg_price=20)]
    wl_ = wl(["A","B","C"])
    sec = {"A":"1","B":"2","C":"3"}
    ltp = {"A":100,"B":100,"C":100}
    plan = build_plan(run_id="T", holdings=holdings, free_cash=1000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg)
    # should not crash, aggregated total 150
    assert plan.nav == 150*100 + 1000

def test_planner_weight_cap_zero_means_no_cap():
    cfg = make_cfg({"portfolio": {"max_weight_per_stock_pct": 0}})
    wl_ = wl(["A"])
    # single name, slice would be 100%, but cap 0 should mean inf (no cap) per fix?
    # Actually our code treats cap 0 as None (no cap)
    plan = build_plan(run_id="T", holdings=[], free_cash=100000, watchlist=wl_, ltp={"A":100}, security_ids={"A":"1"}, cfg=cfg)
    # should not cap - slice 100000
    assert plan.slice_value == 100000

def test_planner_overflow_correct_sizing():
    # holdings OV 500 @900, need overflow sizing not inflated
    import copy
    cfg = copy.deepcopy(CFG_BASE)
    cfg["portfolio"]["n_stocks"] = 2
    cfg["portfolio"]["exit_rank_threshold"] = 2
    wl_ = [TargetName(rank=1, symbol="T1", ref_ltp=100), TargetName(rank=2, symbol="T2", ref_ltp=100), TargetName(rank=3, symbol="OV", ref_ltp=100)]
    holdings = [Position("OV","OV1",500,500, avg_price=900)]
    ltp = {"T1":100,"T2":100,"OV":100}
    sec = {"T1":"1","T2":"2","OV":"OV1"}
    # big cash so overflow should use leftover correctly without under-buy due cost
    plan = build_plan(run_id="T", holdings=holdings, free_cash=50000, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg)
    # check no wash trade (no symbol both sides)
    for sym in ["T1","T2","OV"]:
        sides = {o.side for o in plan.orders if o.symbol==sym}
        assert not (Side.BUY in sides and Side.SELL in sides), f"wash {sym}"

def test_planner_deploy_pct_cliff():
    cfg = make_cfg()
    # deploy 1.0 should be 100% (legacy compat), 1.001 should be ~1%
    import copy
    c1 = copy.deepcopy(cfg)
    c1["portfolio"]["deploy_mode"] = "pct"
    c1["portfolio"]["deploy_pct"] = 1.0
    cap1, _ = resolve_deploy(c1["portfolio"], 100000)
    assert cap1 == 100000
    c2 = copy.deepcopy(cfg)
    c2["portfolio"]["deploy_mode"] = "pct"
    c2["portfolio"]["deploy_pct"] = 1.5
    cap2, _ = resolve_deploy(c2["portfolio"], 100000)
    # 1.5 >1 => 1.5% => 1500
    assert abs(cap2 - 1500) < 1

def test_planner_min_trade_nan_guard():
    cfg = make_cfg()
    cfg["portfolio"]["n_stocks"] = "auto"
    wl_ = wl(["A","B"])
    # slice NaN case shouldn't crash and min_trade should fallback
    plan = build_plan(run_id="T", holdings=[], free_cash=10000, watchlist=wl_, ltp={"A":100,"B":100}, security_ids={"A":"1","B":"2"}, cfg=cfg)
    assert not any("NaN" in str(b) for b in plan.blockers)

def test_planner_resolve_n_float_string():
    assert resolve_n({"n_stocks":"10.0"}, 5) == 10
    assert resolve_n({"n_stocks":None}, 5) == 5  # auto fallback

def test_planner_net_proceeds_floor():
    # dust sells where DP > value should not make budget negative infinite
    cfg = make_cfg()
    holdings = [Position(f"S{i}", f"ID{i}", 1,1, avg_price=10) for i in range(10)]
    wl_ = wl([f"S{i}" for i in range(10)])
    # all held are in wl, but we add extra small holding to sell
    holdings_extra = holdings + [Position("D1","D1",1,1), Position("D2","D2",1,1)]
    # use very low price 5, DP 14.75 > value
    sec = {t.symbol:"1" for t in wl_}
    sec.update({"D1":"D1","D2":"D2", "S0":"ID0"})
    ltp = {t.symbol:100 for t in wl_}
    ltp.update({"D1":5,"D2":5})
    # but D1 D2 not in list? Actually they will be exit; create wl without D1 D2
    plan = build_plan(run_id="T", holdings=holdings_extra, free_cash=0, watchlist=wl_, ltp=ltp, security_ids=sec, cfg=cfg)
    # should not have budget negative overflow - plan should exist
    assert plan.nav >= 0

def test_executor_cash_fit_uses_limit_price():
    from rebalancer.executor import Executor
    from rebalancer.store import Store
    import tempfile
    from pathlib import Path
    cfg = make_cfg()
    cfg["execution"]["limit_buffer_pct"] = 0.01  # 1% buffer
    # make buys where limit vs ref matters: ref 100, limit 101, cash 250 should fit 2*101=202 but ref says 200 fits -> limit matters
    buys = [PlannedOrder(symbol="A", security_id="1", side=Side.BUY, qty=1, ref_price=100, reason=Reason.ENTRY, limit_price=101),
            PlannedOrder(symbol="B", security_id="2", side=Side.BUY, qty=1, ref_price=100, reason=Reason.ENTRY, limit_price=101),
            PlannedOrder(symbol="C", security_id="3", side=Side.BUY, qty=1, ref_price=100, reason=Reason.ENTRY, limit_price=101)]
    plan = type("P", (), {"sells":[], "buys": buys, "warnings":[], "free_cash":0, "nav":1000, "slice_value":100})()
    # mock client with cash 205 (enough for 2 limits 202 but not 3*101=303)
    class MockC:
        def available_cash(self): return 205
    store = Store(Path(tempfile.gettempdir()) / "test_exec_tmp.db")
    exe = Executor(MockC(), store, cfg, dry_run=False)
    fitted = exe._fit_to_available_cash(buys, plan)
    # should have 2 buys, not 3
    assert len(fitted) <= 2

def test_prices_stale_filter():
    from rebalancer.prices import fetch, Quote
    # mock fetchers to return stale quote
    from unittest.mock import patch
    old = Quote(symbol="A", ltp=100, source="yahoo", age_sec=2000)  # 33min > 15
    with patch("rebalancer.prices.fetch_yahoo", return_value={"A": old}):
        with patch("rebalancer.prices.fetch_nse", return_value={}):
            res = fetch(["A"], order=["yahoo"], timeout=1, max_age_sec=900)
            # stale should be filtered => missing
            assert "A" not in res.quotes
            assert "A" in res.missing

def test_config_validation():
    import tempfile, yaml
    cfg = cfgmod.load(ROOT / "config.yaml")
    # bad n_stocks string "10.0" should now pass via our improved resolver but load should still accept?
    # Instead test ConfigError for bad allowed_window format
    bad = cfg.copy()
    # create tmp yaml with bad window
    import copy
    bc = copy.deepcopy(cfg)
    bc["risk"]["allowed_window"] = ["bad", "time"]
    p = Path(tempfile.gettempdir()) / "badwin.yaml"
    p.write_text(yaml.safe_dump({k: v for k,v in bc.items() if k != "_root"}))
    try:
        cfgmod.load(p)
        assert False, "should raise"
    except cfgmod.ConfigError as e:
        assert "allowed_window" in str(e)

def test_store_filled_max():
    from rebalancer.store import Store
    import tempfile
    from pathlib import Path
    db = Store(Path(tempfile.gettempdir()) / f"store_test_{Path(__file__).stem}.db")
    db.record_order(run_id="R1", correlation_id="CID1", symbol="A", side="BUY", planned_qty=10, filled_qty=5, status="PART")
    db.record_order(run_id="R1", correlation_id="CID1", symbol="A", side="BUY", planned_qty=10, filled_qty=3, status="PART")
    row = db.order_already_sent("CID1")
    assert row["filled_qty"] == 5  # max preserved

def test_watchlist_duplicate_detection():
    import tempfile
    from pathlib import Path
    from rebalancer import watchlist as wlmod
    p = Path(tempfile.gettempdir()) / "dup_test.csv"
    p.write_text("NSE Code,Sl No\nRELIANCE,1\nRELIANCE,2\n", encoding="utf-8")
    try:
        wlmod.read(p)
        assert False
    except wlmod.WatchlistError as e:
        assert "duplicate" in str(e).lower()

def test_instruments_atomic_download_fallback(tmp_path):
    # ensure _download fallback uses stale cache tested via _download logic not network
    from rebalancer.instruments import _download
    from pathlib import Path
    dest = tmp_path / "cache.csv"
    dest.write_text("a,b\n1,2\n")
    # set old time >12h
    import time, os
    old = time.time() - 13*3600
    os.utime(dest, (old, old))
    # mock requests.get to fail, should fallback to stale cache
    from unittest.mock import patch, Mock
    import requests
    mock_resp = Mock()
    mock_resp.raise_for_status.side_effect = requests.RequestException("fail")
    with patch("rebalancer.instruments.requests.get", return_value=mock_resp):
        # also patch time? Should still fallback
        text = _download("http://example.com/fake.csv", dest, max_age_hours=12)
        assert "a,b" in text

def test_executor_reconcile_zero_slice():
    from rebalancer.executor import Executor
    from rebalancer.store import Store
    from rebalancer.models import Plan
    import tempfile
    from pathlib import Path
    cfg = make_cfg()
    plan = Plan(run_id="T", nav=10000, free_cash=1000, slice_value=0)
    class MockC2:
        def holdings(self): return [Position("A","1",10,10, avg_price=90)]
        def ltp(self, ids): return {"1": 100}
        def available_cash(self): return 500
    db = Store(Path(tempfile.gettempdir()) / "reconcile_test.db")
    exe = Executor(MockC2(), db, cfg, dry_run=False)
    rec = exe.reconcile(plan)
    assert rec["nav"] >= 0
    # should not divide by zero
    assert rec["positions"][0]["drift_vs_target_pct"] == 0

def test_planner_liquidation_missing_security_block():
    cfg = make_cfg()
    holdings = [Position("A","SEC_A",10,10)]
    plan = __import__("rebalancer.planner", fromlist=["build_liquidation_plan"]).build_liquidation_plan(
        run_id="L1", holdings=holdings, free_cash=0, ltp={"A":100}, security_ids={}, cfg=cfg)
    assert any("securityId" in b or "security" in b.lower() for b in plan.blockers)

def test_circuit_narrow_warning():
    cfg = make_cfg()
    wl_ = wl(["A"])
    ci = {"A": CircuitInfo("A", ltp=100, upper=102, lower=98, prev_close=100)}  # band 2% -> 2% half
    plan = build_plan(run_id="T", holdings=[], free_cash=10000, watchlist=wl_, ltp={"A":100}, security_ids={"A":"1"}, cfg=cfg, circuit=ci)
    assert any("circuit band" in w.lower() for w in plan.warnings)

def test_deploy_amount_zero():
    cfg = make_cfg({"portfolio": {"deploy_mode":"amount","deploy_amount":0}})
    # deploy 0 => investable 0 => slice 0 => no buys?
    wl_ = wl(["A","B"])
    plan = build_plan(run_id="T", holdings=[], free_cash=10000, watchlist=wl_, ltp={"A":100,"B":100}, security_ids={"A":"1","B":"2"}, cfg=cfg)
    assert plan.target_equity == 0
    assert plan.slice_value == 0
    # should have no buys or all skipped
    assert len([o for o in plan.orders if o.side==Side.BUY]) == 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
