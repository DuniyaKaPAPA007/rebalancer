"""Config loading + fail-fast validation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REQUIRED = {
    "dhan": ["client_id_env", "access_token_env", "base_url",
             "exchange_segment", "product_type"],
    "portfolio": ["n_stocks", "exit_rank_threshold", "drift_band_pct",
                  "cash_reserve_pct", "use_overflow_slot"],
    "costs": ["min_trade_value_inr", "min_trade_pct_of_slice",
              "est_sell_cost_pct", "est_buy_cost_pct", "dp_charge_per_scrip_inr"],
    "execution": ["order_type", "limit_buffer_pct", "market_fallback_after_sec",
                  "fill_poll_interval_sec", "fill_wait_timeout_sec", "phase_gap_sec"],
    "risk": ["max_turnover_pct", "max_single_order_value_inr",
             "min_price_inr", "allowed_window"],
    "paths": ["watchlist", "db", "plans_dir", "instruments_cache"],
}


class ConfigError(RuntimeError):
    pass


def load(path: str | Path = "config.yaml") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p} nahi mila.")
    cfg = yaml.safe_load(p.read_text()) or {}

    for section, keys in REQUIRED.items():
        if section not in cfg:
            raise ConfigError(f"config.yaml mein '{section}' section missing hai.")
        for k in keys:
            if k not in cfg[section]:
                raise ConfigError(f"config.yaml: {section}.{k} missing hai.")

    # n_stocks validation with proper error handling
    try:
        n_raw = str(cfg["portfolio"]["n_stocks"]).strip().lower()
        e_raw = str(cfg["portfolio"]["exit_rank_threshold"]).strip().lower()
    except Exception as e:
        raise ConfigError(f"n_stocks/exit_rank_threshold parse fail: {e}")
    if n_raw != "auto":
        try:
            # handle "10.0" case
            n_val = cfg["portfolio"]["n_stocks"]
            if isinstance(n_val, str) and "." in n_val.strip():
                n = int(float(n_val.strip()))
            else:
                n = int(float(n_val) if isinstance(n_val, float) else int(n_val))
        except (ValueError, TypeError, OverflowError) as e:
            raise ConfigError(f"n_stocks '{cfg['portfolio']['n_stocks']}' number nahi hai: {e}")
        if n < 1:
            raise ConfigError("n_stocks kam se kam 1 hona chahiye (ya 'auto').")
        if e_raw != "auto":
            try:
                er_val = cfg["portfolio"]["exit_rank_threshold"]
                if isinstance(er_val, str) and "." in er_val.strip():
                    er = int(float(er_val.strip()))
                else:
                    er = int(float(er_val) if isinstance(er_val, float) else int(er_val))
            except (ValueError, TypeError, OverflowError) as e:
                raise ConfigError(f"exit_rank_threshold '{cfg['portfolio']['exit_rank_threshold']}' invalid: {e}")
            if er < n:
                raise ConfigError(
                    "exit_rank_threshold, n_stocks se chhota nahi ho sakta "
                    "(warna jo naam abhi khareeda wahi turant exit ho jaayega).")
    try:
        cr = float(cfg["portfolio"]["cash_reserve_pct"])
        if cr != cr or cr < 0 or cr >= 0.5 or cr != abs(cr) or not (0 <= cr < 0.5):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ConfigError("cash_reserve_pct 0 aur 0.5 ke beech rakho (0.01 = 1%).")
    # ---- deploy budget (optional -- purani config.yaml bhi chalti rahe) --
    # Galat spelling par CHUP-CHAP "all" par mat gir jao. "all" ka matlab
    # poora paisa market mein, aur wo user ki marzi ke khilaaf ho sakta hai.
    pf = cfg["portfolio"]
    pf.setdefault("deploy_mode", "all")
    pf.setdefault("deploy_pct", 1.0)
    pf.setdefault("deploy_amount", 0)
    dm = str(pf["deploy_mode"]).strip().lower()
    if dm not in ("all", "pct", "percent", "percentage", "%",
                  "amount", "amt", "rupees", "inr", "fixed"):
        raise ConfigError(
            f"deploy_mode '{pf['deploy_mode']}' samajh nahi aaya. "
            f"Sirf all / pct / amount chalega.")
    try:
        dp = float(pf["deploy_pct"] or 0)
        da = float(pf["deploy_amount"] or 0)
    except (TypeError, ValueError):
        raise ConfigError("deploy_pct / deploy_amount number hone chahiye.")
    if dp < 0 or da < 0:
        raise ConfigError("deploy_pct / deploy_amount negative nahi ho sakte.")
    if dm in ("pct", "percent", "percentage", "%") and dp > 100:
        raise ConfigError(
            f"deploy_pct {dp} -- 100% se zyada nahi laga sakte. "
            f"(0.60 ya 60 likho 60% ke liye.)")

    if cfg["execution"]["order_type"] not in ("LIMIT", "MARKET"):
        raise ConfigError("order_type sirf LIMIT ya MARKET ho sakta hai.")
    # validate execution numeric params
    for k in ("limit_buffer_pct", "market_fallback_after_sec", "fill_poll_interval_sec", "fill_wait_timeout_sec", "phase_gap_sec", "max_plan_age_min"):
        if k in cfg["execution"]:
            try:
                v = cfg["execution"][k]
                fv = float(v)
                if fv != fv or fv < 0 or fv != abs(fv) or (fv != 0 and fv != fv):
                    raise ValueError
                if k == "limit_buffer_pct" and fv > 0.1:
                    raise ConfigError(f"{k} {fv} bahut bada >10%")
            except ConfigError:
                raise
            except (TypeError, ValueError, OverflowError):
                raise ConfigError(f"execution.{k} numeric hona chahiye, mila {cfg['execution'][k]}")

    # prices section optional hai -- purani config.yaml bhi chalti rahe
    pr = cfg.setdefault("prices", {})
    pr.setdefault("fallback", ["yahoo", "nse"])
    pr.setdefault("stale_warn_min", 20)
    try:
        sw = float(pr.get("stale_warn_min", 20) or 20)
        if sw != sw or sw <= 0 or sw > 1440 or not (0 < sw < 10000):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ConfigError("prices.stale_warn_min 1-1440 ke beech hona chahiye")
    if isinstance(pr["fallback"], str):
        pr["fallback"] = [x.strip() for x in pr["fallback"].split(",") if x.strip()]
    # validate fallback entries
    valid_fb = {"yahoo", "nse"}
    for fb in pr["fallback"]:
        if str(fb).strip().lower() not in valid_fb:
            raise ConfigError(f"prices.fallback '{fb}' invalid, sirf yahoo/nse")

    # risk validation
    for k in ("max_turnover_pct", "max_single_order_value_inr", "min_price_inr"):
        if k in cfg["risk"]:
            try:
                fv = float(cfg["risk"][k])
                if fv != fv or fv < 0 or fv != abs(fv):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise ConfigError(f"risk.{k} numeric >=0 hona chahiye")
    if "allowed_window" in cfg["risk"]:
        aw = cfg["risk"]["allowed_window"]
        if not isinstance(aw, (list, tuple)) or len(aw) != 2:
            raise ConfigError("risk.allowed_window ['09:45','15:00'] format me hona chahiye")
        import re as _re2
        for t in aw:
            if not isinstance(t, str) or not _re2.match(r"^\d{2}:\d{2}$", t.strip()):
                raise ConfigError(f"allowed_window time '{t}' HH:MM hona chahiye")
    # size guard for config
    try:
        if p.stat().st_size > 1024 * 1024:
            raise ConfigError("config.yaml bahut badi >1MB, galat file?")
    except OSError:
        pass

    # config-relative paths, taaki kahin se bhi CLI chala sako
    root = p.resolve().parent
    for k, v in cfg["paths"].items():
        # prevent absolute path traversal outside project? Allow but warn
        vp = Path(v)
        if vp.is_absolute():
            # allow absolute but ensure it's not system path like /etc/passwd? just allow
            cfg["paths"][k] = str(vp.resolve())
        else:
            cfg["paths"][k] = str((root / vp).resolve())
    cfg["_root"] = str(root)
    return cfg


def credentials(cfg: dict) -> tuple[str, str]:
    cid = os.environ.get(cfg["dhan"]["client_id_env"], "")
    tok = os.environ.get(cfg["dhan"]["access_token_env"], "")
    if not cid or not tok:
        raise ConfigError(
            f"Environment variables set karo:\n"
            f"  export {cfg['dhan']['client_id_env']}=...\n"
            f"  export {cfg['dhan']['access_token_env']}=...")
    return cid, tok
