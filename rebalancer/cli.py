"""
CLI. Do-step flow -- jaan-bujh ke.

    python -m rebalancer.cli plan                    # plan banao aur dikhao
    python -m rebalancer.cli execute --run-id ... --approve

Plan aur execute alag isliye hain ki tum har order apni aankh se dekh sako
paisa lagne se pehle. Cron isko `plan` tak hi chalaye -- execute tum karo.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import instruments, report
from . import watchlist as wlmod
from .dhan import DhanClient, DhanError
from .models import Plan, PlannedOrder, Position, Reason, Side, Skipped, TargetName
from .planner import build_plan
from .store import Store, plan_to_json

log = logging.getLogger("rebalancer")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")


# ----------------------------------------------------------------------
def read_watchlist(path: str, rank_by: str = "file_order") -> list[TargetName]:
    """watchlist module ka thin wrapper (simple + screener dono formats)."""
    return wlmod.read(path, rank_by=rank_by)


def _check_window(cfg: dict, force: bool) -> None:
    start, end = cfg["risk"]["allowed_window"]
    now = datetime.now(IST)
    if now.weekday() >= 5:
        msg = f"Aaj {now:%A} hai -- market band."
        if not force:
            raise SystemExit(f"{msg} (--force se bypass)")
        log.warning(msg)
    hhmm = now.strftime("%H:%M")
    if not (start <= hhmm <= end):
        msg = (f"Abhi {hhmm} IST hai, allowed window {start}-{end} hai. "
               f"Open/close ke paas spread kharab hota hai.")
        if not force:
            raise SystemExit(f"{msg} (--force se bypass)")
        log.warning(msg)


# ----------------------------------------------------------------------
def cmd_plan(args) -> int:
    cfg = cfgmod.load(args.config)
    p_cfg = cfg["portfolio"]

    watchlist = read_watchlist(cfg["paths"]["watchlist"],
                               rank_by=p_cfg.get("rank_by", "file_order"))
    log.info("Watchlist: %d naam, top-%d = %s", len(watchlist),
             p_cfg["n_stocks"],
             ", ".join(t.symbol for t in watchlist[:p_cfg["n_stocks"]]))

    pre_warnings = wlmod.sanity_checks(
        watchlist, int(p_cfg["n_stocks"]), bool(p_cfg["use_overflow_slot"]),
        float(cfg["risk"].get("min_market_cap_cr", 0)))

    client = DhanClient(*cfgmod.credentials(cfg), base_url=cfg["dhan"]["base_url"])
    db = Store(cfg["paths"]["db"])

    log.info("Scrip master load kar rahe hain...")
    sym_map, isin_map = instruments.load_equity_maps(cfg["paths"]["instruments_cache"])

    log.info("Holdings aur funds fetch kar rahe hain...")
    holdings = client.holdings()
    cash = client.available_cash()
    log.info("%d holdings, Rs.%.2f free cash", len(holdings), cash)

    # securityId: holdings se seedha, watchlist ke liye ISIN -> phir symbol
    security_ids = {h.symbol: h.security_id for h in holdings}
    found, missing, via_isin = instruments.resolve(watchlist, sym_map, isin_map)
    security_ids.update(found)
    if via_isin:
        log.info("ISIN se match hue (symbol badla hua lagta hai): %s",
                 ", ".join(via_isin))
    if missing:
        log.error("Ye symbols NSE equity master mein nahi mile: %s",
                  ", ".join(missing))
        log.error("Wajah aksar: (a) NSE code galat hai, (b) scrip BE/T2T series "
                  "mein hai, ya (c) sirf BSE-listed hai. CSV mein 'NSE Code' "
                  "column check karo.")

    log.info("LTP aur circuit limits fetch kar rahe hain...")
    symbol_of = {sid: sym for sym, sid in security_ids.items()}
    circuit = client.quotes(sorted(set(security_ids.values())), symbol_of,
                            segment=cfg["dhan"]["exchange_segment"])
    if circuit:
        ltp = {sym: (circuit[sym].ltp if sym in circuit else 0.0)
               for sym in security_ids}
    else:                                   # quote API fail -> LTP par gir jao
        ltp_by_id = client.ltp(sorted(set(security_ids.values())),
                               segment=cfg["dhan"]["exchange_segment"])
        ltp = {sym: ltp_by_id.get(sid, 0.0) for sym, sid in security_ids.items()}

    # CSV baasi toh nahi? Purani list par rebalance = kal ke momentum par
    # aaj ka paisa.
    pre_warnings += wlmod.stale_check(
        watchlist, ltp, float(cfg["risk"].get("stale_ltp_tolerance", 0.05)))

    run_id = datetime.now(IST).strftime("R%Y%m%d-%H%M")
    plan = build_plan(run_id=run_id, holdings=holdings, free_cash=cash,
                      watchlist=watchlist, ltp=ltp, security_ids=security_ids,
                      cfg=cfg, circuit=circuit)
    plan.warnings = pre_warnings + plan.warnings

    db.save_run(run_id, datetime.now(IST).isoformat(timespec="seconds"),
                "BLOCKED" if plan.blockers else "PLANNED",
                plan.nav, plan.free_cash, plan.slice_value, plan_to_json(plan))
    db.snapshot(run_id, "BEFORE",
                [(h.symbol, h.total_qty, ltp.get(h.symbol, 0.0)) for h in holdings],
                datetime.now(IST).isoformat(timespec="seconds"))

    plans_dir = Path(cfg["paths"]["plans_dir"])
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / f"{run_id}.json").write_text(plan_to_json(plan))
    text = report.render(plan, cfg)
    (plans_dir / f"{run_id}.txt").write_text(text)
    print("\n" + text)
    log.info("Plan saved: %s", plans_dir / f"{run_id}.json")
    return 1 if plan.blockers else 0


# ----------------------------------------------------------------------
def _plan_from_json(path: Path) -> Plan:
    d = json.loads(path.read_text())
    p = Plan(run_id=d["run_id"], nav=d["nav"], free_cash=d["free_cash"],
             slice_value=d["slice_value"], warnings=d.get("warnings", []),
             blockers=d.get("blockers", []),
             skipped=[Skipped(**s) for s in d.get("skipped", [])])
    if d.get("created_ts"):
        p.created_ts = float(d["created_ts"])
    p.is_liquidation = bool(d.get("is_liquidation"))
    p.orders = [PlannedOrder(symbol=o["symbol"], security_id=o["security_id"],
                             side=Side(o["side"]), qty=o["qty"],
                             ref_price=o["ref_price"], limit_price=o["limit_price"],
                             reason=Reason(o["reason"]), note=o.get("note", ""))
                for o in d["orders"]]
    return p


def cmd_execute(args) -> int:
    from .executor import Executor

    cfg = cfgmod.load(args.config)
    plans_dir = Path(cfg["paths"]["plans_dir"])

    run_id = args.run_id
    if not run_id:
        # sabse naya plan uthao -- taaki run-id type na karna pade
        found = sorted(plans_dir.glob("R*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if not found:
            raise SystemExit(
                "Koi plan nahi mila. Pehle 2-PLAN.bat chalao (ya `run.bat plan`).")
        run_id = found[0].stem
        log.info("Sabse naya plan: %s", run_id)
    args.run_id = run_id

    plan_file = plans_dir / f"{run_id}.json"
    if not plan_file.exists():
        raise SystemExit(f"Plan nahi mila: {plan_file}. Pehle `plan` chalao.")
    plan = _plan_from_json(plan_file)

    if plan.blockers:
        print("Plan BLOCKED hai:")
        for b in plan.blockers:
            print("  x", b)
        return 1

    dry = not args.approve
    if not dry:
        _check_window(cfg, args.force)

    client = DhanClient(*cfgmod.credentials(cfg), base_url=cfg["dhan"]["base_url"])
    db = Store(cfg["paths"]["db"])

    # --- stale plan guard: price kitna hil gaya? ---------------------
    try:
        fresh = client.ltp(sorted({o.security_id for o in plan.orders}),
                           segment=cfg["dhan"]["exchange_segment"])
        moved = []
        for o in plan.orders:
            now_px = fresh.get(o.security_id)
            if now_px and o.ref_price:
                d = abs(now_px - o.ref_price) / o.ref_price
                if d > 0.02:
                    moved.append(f"{o.symbol} {d*100:.1f}%")
        if moved:
            msg = ("Plan banne ke baad price 2%+ hil chuka hai: "
                   + ", ".join(moved) + ". Naya plan banao.")
            if not args.force:
                raise SystemExit(msg + "  (--force se bypass)")
            log.warning(msg)
    except DhanError as e:
        log.warning("Freshness check nahi ho paaya: %s", e)

    print(report.render(plan, cfg))
    if dry:
        print("\n  *** DRY RUN -- koi order nahi jaayega. "
              "Asli execution ke liye --approve lagao. ***\n")
    else:
        print(f"\n  *** LIVE. {len(plan.sells)} SELL + {len(plan.buys)} BUY "
              f"orders jaayenge. ***")
        if input("  'HAAN' likh ke Enter dabao: ").strip() != "HAAN":
            print("  Cancel kiya.")
            db.set_status(plan.run_id, "ABORTED", "user ne cancel kiya")
            return 1

    result = Executor(client, db, cfg, dry_run=dry).run(plan)
    print(f"\n  Placed: {len(result['sells'])} sell, {len(result['buys'])} buy, "
          f"{len(result['failed'])} failed")
    for f in result["failed"]:
        print(f"    x {f['side']} {f['symbol']}: {f['error']}")
    if result.get("reconciliation"):
        print(report.render_reconciliation(result["reconciliation"]))
    return 0 if not result["failed"] else 1


# ----------------------------------------------------------------------
def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config)
    db = Store(cfg["paths"]["db"])
    if args.run_id:
        for o in db.orders_for(args.run_id):
            print(f"  {o['side']:<5}{o['symbol']:<14}{o['planned_qty']:>6} planned "
                  f"{o['filled_qty'] or 0:>6} filled  {o['status']}"
                  + (f"  {o['error']}" if o["error"] else ""))
    else:
        for r in db.recent_runs(15):
            print(f"  {r['run_id']}  {r['status']:<18} NAV Rs.{r['nav'] or 0:,.0f}")
    return 0


def cmd_holdings(args) -> int:
    cfg = cfgmod.load(args.config)
    client = DhanClient(*cfgmod.credentials(cfg), base_url=cfg["dhan"]["base_url"])
    hs = client.holdings()
    ltp = client.ltp([h.security_id for h in hs]) if hs else {}
    nav = sum(h.total_qty * ltp.get(h.security_id, 0) for h in hs)
    cash = client.available_cash()
    print(f"\n  {'SYMBOL':<14}{'QTY':>7}{'FREE':>7} {'LTP':>10} {'VALUE':>13} {'WT':>7}")
    print("  " + "-" * 60)
    for h in sorted(hs, key=lambda x: -x.total_qty * ltp.get(x.security_id, 0)):
        v = h.total_qty * ltp.get(h.security_id, 0)
        print(f"  {h.symbol:<14}{h.total_qty:>7}{h.available_qty:>7} "
              f"{ltp.get(h.security_id, 0):>10,.2f} {v:>13,.0f} "
              f"{v / (nav + cash) * 100 if nav + cash else 0:>6.1f}%")
    print(f"\n  Holdings Rs.{nav:,.0f}  +  Cash Rs.{cash:,.0f}  =  NAV Rs.{nav + cash:,.0f}\n")
    return 0


# ----------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rebalancer",
                                 description="Weekly equal-weight rebalancer (Dhan)")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plan", help="plan banao (koi order nahi jaata)").set_defaults(fn=cmd_plan)

    e = sub.add_parser("execute", help="plan execute karo")
    e.add_argument("--run-id", help="kaunsa plan (na do toh sabse naya uthega)")
    e.add_argument("--latest", action="store_true",
                   help="sabse naya plan uthao (--run-id ki zarurat nahi)")
    e.add_argument("--approve", action="store_true",
                   help="ye NAHI diya toh dry run hoga")
    e.add_argument("--force", action="store_true",
                   help="time-window aur stale-price guard bypass")
    e.set_defaults(fn=cmd_execute)

    s = sub.add_parser("status", help="runs / orders dekho")
    s.add_argument("--run-id")
    s.set_defaults(fn=cmd_status)

    sub.add_parser("holdings", help="abhi ka portfolio").set_defaults(fn=cmd_holdings)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.fn(args)
    except (cfgmod.ConfigError, DhanError, instruments.ScripMasterError) as ex:
        log.error("%s", ex)
        return 2
    except KeyboardInterrupt:
        log.error("Beech mein rok diya. `status --run-id ...` se dekho kya hua.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
