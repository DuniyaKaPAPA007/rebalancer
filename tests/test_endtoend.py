"""
End-to-end smoke test: config -> plan -> report -> dry-run executor -> store.
Fake broker use karte hain, koi network call nahi.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rebalancer import config as cfgmod        # noqa: E402
from rebalancer import report                  # noqa: E402
from rebalancer.executor import Executor       # noqa: E402
from rebalancer.models import Position, TargetName, Side  # noqa: E402
from rebalancer.planner import build_plan, resolve_n  # noqa: E402
from rebalancer.store import Store, plan_to_json  # noqa: E402
from rebalancer.cli import read_watchlist, _plan_from_json  # noqa: E402


class FakeDhan:
    """Sirf utne methods jitne executor dry-run mein chhoota hai."""
    def __init__(self):
        self.placed = []

    def available_cash(self):
        return 150_000.0

    def holdings(self):
        return []

    def ltp(self, ids, segment="NSE_EQ"):
        return {str(i): 100.0 for i in ids}

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": f"OID{len(self.placed)}", "orderStatus": "TRANSIT"}


@pytest.fixture
def cfg():
    return cfgmod.load(ROOT / "config.yaml")


def test_config_loads_and_validates(cfg):
    # shipped default "auto" hai -- list ka size hi slots decide karta hai
    assert str(cfg["portfolio"]["n_stocks"]).lower() == "auto"
    assert Path(cfg["paths"]["watchlist"]).name == "watchlist.csv"


def test_config_rejects_bad_exit_threshold(tmp_path, cfg):
    import yaml
    bad = dict(cfg)
    bad["portfolio"] = {**cfg["portfolio"], "n_stocks": 10,
                        "exit_rank_threshold": 5}
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(cfgmod.ConfigError, match="exit_rank_threshold"):
        cfgmod.load(p)


def test_config_accepts_auto_and_numbers(tmp_path, cfg):
    import yaml
    for n, e in [("auto", "auto"), (10, 10), (25, 30), (3, "auto"), (60, 60)]:
        c = dict(cfg)
        c["portfolio"] = {**cfg["portfolio"], "n_stocks": n,
                          "exit_rank_threshold": e}
        p = tmp_path / f"c_{n}_{e}.yaml"
        p.write_text(yaml.safe_dump(c))
        cfgmod.load(p)                       # koi exception nahi aana chahiye


def test_shipped_watchlist_parses(cfg):
    wl = read_watchlist(cfg["paths"]["watchlist"])
    assert len(wl) >= 1
    assert all(t.symbol and not t.symbol.isdigit() for t in wl)


def test_scales_to_any_list_size(cfg):
    """3 naam ho ya 100 -- app barabar baant deti hai, bina config badle."""
    from rebalancer.models import TargetName, CircuitInfo
    import copy

    for size in (2, 5, 11, 25, 60, 120):
        wl = [TargetName(rank=i + 1, symbol=f"S{i+1:03d}", ref_ltp=100.0,
                         market_cap_cr=25_000.0) for i in range(size)]
        ltp = {t.symbol: 100.0 for t in wl}
        sec = {t.symbol: f"SEC{i}" for i, t in enumerate(wl)}
        circ = {s: CircuitInfo(symbol=s, ltp=100.0, upper=120.0, lower=80.0,
                               prev_close=100.0, volume=10_00_00_000)
                for s in ltp}
        c = copy.deepcopy(cfg)
        c["portfolio"]["n_stocks"] = "auto"
        c["portfolio"]["exit_rank_threshold"] = "auto"
        c["risk"]["max_single_order_value_inr"] = 10_00_00_000
        plan = build_plan(run_id="T", holdings=[], free_cash=1_00_00_000.0,
                          watchlist=wl, ltp=ltp, security_ids=sec, cfg=c,
                          circuit=circ)
        n_slots = resolve_n(c["portfolio"], size)
        expected = size - 1 if size > 1 else 1        # overflow slot
        assert n_slots == expected, f"{size} naam -> {n_slots} slots"
        assert not plan.blockers, f"{size} naam: {plan.blockers}"
        buys = [o for o in plan.orders if o.side is Side.BUY]
        # har slot bharna chahiye; n+1 slot tabhi bharta hai jab paisa bache
        assert n_slots <= len(buys) <= size, f"{size} naam -> {len(buys)} buys"
        # paisa barabar bata -- sabse bada aur sabse chhota order paas-paas
        vals = sorted(o.qty * o.ref_price for o in buys[:n_slots])
        if len(vals) > 1:
            assert vals[-1] / vals[0] < 1.05, f"{size} naam: weights barabar nahi"


def _make_plan(cfg, holdings=(), cash=15_00_000.0):
    """Asli shipped watchlist + uske apne LTP par plan banao."""
    wl = read_watchlist(cfg["paths"]["watchlist"])
    syms = [t.symbol for t in wl] + [h.symbol for h in holdings]
    sec = {s: f"sec-{s}" for s in syms}
    px = {t.symbol: (t.ref_ltp or 100.0) for t in wl}
    px.update({h.symbol: 250.0 for h in holdings})
    return build_plan(run_id="RTEST", holdings=list(holdings), free_cash=cash,
                      watchlist=wl, ltp=px, security_ids=sec, cfg=cfg)


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (500, "500"), (1_000, "1,000"), (99_999, "99,999"),
    (1_00_000, "1,00,000"), (1_74_800, "1,74,800"),
    (1_23_45_678, "1,23,45,678"), (-45_000, "-45,000"),
])
def test_indian_number_format(value, expected):
    assert report._inr(value) == expected


def test_report_renders_without_error(cfg):
    plan = _make_plan(cfg)
    text = report.render(plan, cfg)
    for section in ("REBALANCE PLAN", "ESTIMATED COSTS", "BUY", "Turnover",
                    "DP charges"):
        assert section in text, section


def test_plan_json_roundtrip(cfg, tmp_path):
    plan = _make_plan(cfg, holdings=[Position("OLDNAME", "sec-OLDNAME", 100, 100)])
    js = plan_to_json(plan)
    p = tmp_path / "RTEST.json"
    p.write_text(js)
    back = _plan_from_json(p)

    assert back.run_id == plan.run_id
    assert len(back.orders) == len(plan.orders)
    assert {o.symbol for o in back.sells} == {o.symbol for o in plan.sells}
    # SELL abhi bhi BUY se pehle hai
    sides = [o.side.value for o in back.ordered()]
    assert sides == sorted(sides, key=lambda s: 0 if s == "SELL" else 1)


def test_dry_run_executor_places_nothing(cfg, tmp_path):
    plan = _make_plan(cfg, holdings=[Position("OLDNAME", "sec-OLDNAME", 100, 100)])
    db = Store(tmp_path / "t.db")
    db.save_run(plan.run_id, "2026-08-17T10:00:00", "PLANNED", plan.nav,
                plan.free_cash, plan.slice_value, plan_to_json(plan))

    broker = FakeDhan()
    result = Executor(broker, db, cfg, dry_run=True).run(plan)

    assert broker.placed == []                       # kuch bheja hi nahi
    assert not result["failed"]
    rows = db.orders_for(plan.run_id)
    assert rows and all(r["status"] == "DRY_RUN" for r in rows)
    assert len(rows) == len(plan.orders)


def test_store_blocks_duplicate_correlation_id(tmp_path):
    db = Store(tmp_path / "t.db")
    db.record_order(run_id="R1", correlation_id="R1-BTCS", symbol="TCS",
                    side="BUY", planned_qty=10, status="TRANSIT",
                    broker_order_id="X1")
    db.record_order(run_id="R1", correlation_id="R1-BTCS", symbol="TCS",
                    side="BUY", planned_qty=10, status="TRADED",
                    broker_order_id="X1")
    assert len(db.orders_for("R1")) == 1             # ek hi row, update hui
    assert db.order_already_sent("R1-BTCS")["status"] == "TRADED"


def test_correlation_id_is_within_dhan_30_char_limit(cfg):
    plan = _make_plan(cfg)
    for o in plan.orders:
        cid = o.correlation_id("R20260817-1030")
        assert len(cid) <= 30, cid
    cids = [o.correlation_id("R20260817-1030") for o in plan.orders]
    assert len(set(cids)) == len(cids)               # sab unique


def test_executor_refuses_blocked_plan(cfg, tmp_path):
    plan = _make_plan(cfg)
    plan.blockers.append("test blocker")
    db = Store(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="blocked"):
        Executor(FakeDhan(), db, cfg, dry_run=True).run(plan)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
