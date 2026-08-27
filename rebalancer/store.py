"""
SQLite audit log.

Yaad rakho: ye DB kabhi bhi truth ka source NAHI hai. Quantity hamesha
Dhan holdings API se aati hai (bonus/split/merger DB mein reflect nahi hote).
Ye sirf "kya hua tha" ka record hai -- debugging, tax aur reconciliation ke liye.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- PLANNED|APPROVED|EXECUTING|DONE|ABORTED
    nav         REAL,
    free_cash   REAL,
    slice_value REAL,
    plan_json   TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    correlation_id TEXT UNIQUE,          -- duplicate order ka DB-level guard
    broker_order_id TEXT,
    symbol         TEXT NOT NULL,
    security_id    TEXT,
    side           TEXT NOT NULL,
    reason         TEXT,
    planned_qty    INTEGER NOT NULL,
    filled_qty     INTEGER DEFAULT 0,
    limit_price    REAL,
    avg_fill_price REAL,
    status         TEXT,
    placed_at      TEXT,
    updated_at     TEXT,
    error          TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id);

CREATE TABLE IF NOT EXISTS holdings_snapshot (
    run_id     TEXT NOT NULL,
    phase      TEXT NOT NULL,           -- BEFORE|AFTER
    symbol     TEXT NOT NULL,
    qty        INTEGER,
    ltp        REAL,
    captured_at TEXT
);

-- Zerodha-like NAV tracking (real, not fake)
CREATE TABLE IF NOT EXISTS nav_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,          -- ISO timestamp
    nav          REAL NOT NULL,
    holdings_value REAL NOT NULL,
    free_cash    REAL NOT NULL,
    realized_pnl REAL DEFAULT 0,
    source       TEXT,                    -- Dhan / paper / manual
    UNIQUE(captured_at)
);
CREATE INDEX IF NOT EXISTS idx_nav_captured ON nav_history(captured_at);

CREATE TABLE IF NOT EXISTS fund_flows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_date    TEXT NOT NULL,          -- ISO date
    amount       REAL NOT NULL,          -- +ve deposit, -ve withdraw
    flow_type    TEXT NOT NULL,          -- DEPOSIT / WITHDRAW
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flows_date ON fund_flows(flow_date);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # enable WAL for better concurrency
            try:
                c.execute("PRAGMA journal_mode=WAL;")
                c.execute("PRAGMA synchronous=NORMAL;")
                c.execute("PRAGMA busy_timeout=30000;")
                c.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);")
            except Exception:
                pass

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            yield conn
            try:
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- runs ---------------------------------------------------------
    def save_run(self, run_id: str, created_at: str, status: str,
                 nav: float, free_cash: float, slice_value: float,
                 plan_json: str, notes: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (run_id, created_at, status, nav, free_cash,"
                " slice_value, plan_json, notes) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,"
                " plan_json=excluded.plan_json, notes=excluded.notes",
                (run_id, created_at, status, nav, free_cash, slice_value,
                 plan_json, notes))

    def set_status(self, run_id: str, status: str, notes: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET status=?, notes=COALESCE(NULLIF(?,''),notes)"
                      " WHERE run_id=?", (status, notes, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT run_id, created_at, status, nav FROM runs"
                " ORDER BY created_at DESC LIMIT ?", (limit,))]

    # ---- orders -------------------------------------------------------
    def order_already_sent(self, correlation_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM orders WHERE correlation_id=?",
                          (correlation_id,)).fetchone()
            return dict(r) if r else None

    def record_order(self, **kw: Any) -> None:
        # sanitize cols - only allow known columns to avoid injection
        allowed = {"run_id", "correlation_id", "broker_order_id", "symbol", "security_id",
                   "side", "reason", "planned_qty", "filled_qty", "limit_price", "avg_fill_price",
                   "status", "placed_at", "updated_at", "error"}
        kw = {k: v for k, v in kw.items() if k in allowed}
        if not kw:
            return
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        # don't overwrite run_id on conflict, also protect filled_qty from going backwards
        # Use max for filled_qty via CASE, but simple: only update if new filled > old
        # For now exclude run_id from update to preserve first association
        upd_parts = []
        for k in kw:
            if k in ("correlation_id", "run_id"):
                continue
            if k == "filled_qty":
                upd_parts.append(f"{k}=MAX(COALESCE({k},0), excluded.{k})")
            else:
                upd_parts.append(f"{k}=excluded.{k}")
        upd = ", ".join(upd_parts) if upd_parts else "updated_at=excluded.updated_at"
        with self._conn() as c:
            c.execute(f"INSERT INTO orders ({cols}) VALUES ({marks})"
                      f" ON CONFLICT(correlation_id) DO UPDATE SET {upd}",
                      tuple(kw.values()))

    def orders_for(self, run_id: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE run_id=? ORDER BY id", (run_id,))]

    # ---- snapshots ----------------------------------------------------
    def snapshot(self, run_id: str, phase: str, rows: list[tuple], at: str) -> None:
        with self._conn() as c:
            c.executemany(
                "INSERT INTO holdings_snapshot (run_id, phase, symbol, qty, ltp,"
                " captured_at) VALUES (?,?,?,?,?,?)",
                [(run_id, phase, s, q, p, at) for s, q, p in rows])

    # ---- NAV history (Zerodha-like, real) -------------------------------
    def record_nav(self, nav: float, holdings_value: float, free_cash: float, source: str = "Dhan", at: str | None = None) -> None:
        from datetime import datetime
        from rebalancer.tz import IST
        if at is None:
            at = datetime.now(IST).isoformat(timespec="seconds")
        # avoid duplicate within same minute - use minute resolution
        # check if recent nav within 5 min is same (avoid spamming)
        with self._conn() as c:
            cur = c.execute("SELECT captured_at, nav FROM nav_history ORDER BY captured_at DESC LIMIT 1").fetchone()
            if cur:
                # if last record < 2 min ago and nav same within 0.1%, skip
                try:
                    import datetime as dt
                    last_dt = dt.datetime.fromisoformat(cur["captured_at"].replace("Z","+00:00"))
                    # naive compare by string prefix minute?
                    if cur["captured_at"][:16] == at[:16] and abs(cur["nav"] - nav) / max(nav,1) < 0.001:
                        return
                except:
                    pass
            c.execute("INSERT OR IGNORE INTO nav_history (captured_at, nav, holdings_value, free_cash, source) VALUES (?,?,?,?,?)",
                      (at, nav, holdings_value, free_cash, source))

    def get_nav_history(self, limit: int = 120, timeframe: str = "daily") -> list[dict]:
        # timeframe: daily / weekly / monthly / yearly
        # fetch enough raw rows to cover resampling (yearly needs more)
        fetch_mult = {"daily": 1, "weekly": 7, "monthly": 31, "yearly": 366}.get(timeframe, 7)
        with self._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM nav_history ORDER BY captured_at ASC LIMIT ?", (limit*fetch_mult,))]
        if not rows:
            return []
        from collections import OrderedDict
        if timeframe == "weekly":
            # group by ISO year-week, take last per week
            grouped = OrderedDict()
            for r in rows:
                try:
                    import datetime as dt
                    d = dt.datetime.fromisoformat(r["captured_at"].replace("Z","+00:00"))
                    key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                    grouped[key] = r
                except:
                    grouped[r["captured_at"][:10]] = r
            rows = list(grouped.values())[-limit:]
        elif timeframe == "monthly":
            grouped = OrderedDict()
            for r in rows:
                try:
                    key = r["captured_at"][:7]  # YYYY-MM
                    grouped[key] = r
                except:
                    grouped[r["captured_at"][:10]] = r
            rows = list(grouped.values())[-limit:]
        elif timeframe == "yearly":
            grouped = OrderedDict()
            for r in rows:
                try:
                    key = r["captured_at"][:4]  # YYYY
                    grouped[key] = r
                except:
                    grouped[r["captured_at"][:10]] = r
            rows = list(grouped.values())[-limit:]
        else:
            # daily: group by date, take last per day
            grouped = OrderedDict()
            for r in rows:
                key = r["captured_at"][:10]
                grouped[key] = r
            rows = list(grouped.values())[-limit:]
        return rows

    def calc_ema(self, values: list[float], period: int) -> list[float | None]:
        if period <=1 or not values:
            return [None]*len(values)
        k = 2/(period+1)
        ema = []
        # simple SMA for first period as seed
        for i, v in enumerate(values):
            if i==0:
                ema.append(v)
            elif i < period:
                # SMA seed
                ema.append(sum(values[:i+1])/(i+1))
            else:
                ema.append(v*k + ema[-1]*(1-k))
        # first period-1 are less reliable, return None for them if strict? But keep for chart
        return ema

    # ---- fund flows (withdraw/deposit) ---------------------------------
    def add_fund_flow(self, amount: float, flow_type: str, note: str = "", flow_date: str | None = None) -> int:
        from datetime import datetime
        from rebalancer.tz import IST
        if flow_date is None:
            flow_date = datetime.now(IST).isoformat(timespec="seconds")
        created_at = datetime.now(IST).isoformat(timespec="seconds")
        flow_type = flow_type.upper()
        if flow_type not in ("DEPOSIT","WITHDRAW"):
            flow_type = "DEPOSIT" if amount>0 else "WITHDRAW"
        with self._conn() as c:
            cur = c.execute("INSERT INTO fund_flows (flow_date, amount, flow_type, note, created_at) VALUES (?,?,?,?,?)",
                      (flow_date, amount, flow_type, note, created_at))
            return cur.lastrowid

    def get_fund_flows(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM fund_flows ORDER BY flow_date DESC LIMIT ?", (limit,))]

    def get_realized_pnl(self) -> float:
        # rough: sum of sell proceeds - buy costs from orders where status TRADED? Not precise without avg.
        # For now return 0, to be enhanced with actual trade history
        return 0.0


def plan_to_json(plan) -> str:
    return json.dumps({
        "run_id": plan.run_id,
        "created_ts": getattr(plan, "created_ts", None),
        "is_liquidation": getattr(plan, "is_liquidation", False),
        "nav": plan.nav,
        "free_cash": plan.free_cash,
        "slice_value": plan.slice_value,
        "orders": [{"symbol": o.symbol, "security_id": o.security_id,
                    "side": o.side.value, "qty": o.qty, "ref_price": o.ref_price,
                    "limit_price": o.limit_price, "reason": o.reason.value,
                    "note": o.note} for o in plan.ordered()],
        "skipped": [{"symbol": s.symbol, "reason": s.reason} for s in plan.skipped],
        "warnings": plan.warnings,
        "blockers": plan.blockers,
    }, indent=2)
