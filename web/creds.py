"""Dhan credentials -- verify aur locally save.

SURAKSHA ke bare mein do baatein, saaf-saaf:

  * Token kabhi log nahi hota. API response mein bhi sirf aakhri 4 characters
    jaate hain (masking ke liye), poora token kabhi wapas nahi jaata.
  * Server sirf 127.0.0.1 par sunta hai. Token yahan se sirf ek hi jagah
    jaata hai -- Dhan ki apni API par. Aur kahin nahi.
  * Save karoge toh creds.bat mein jaata hai, tumhare apne folder mein.
    Wo file .gitignore mein hai.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from rebalancer.tz import IST

CREDS_FILE = "creds.bat"

_CLIENT_RE = re.compile(r"^[0-9]{6,20}$")
# Dhan khaali portfolio ko bhi kabhi-kabhi 5xx bhej deta hai
_NO_DATA_RE = re.compile(
    r"no.?data|data.?missing|no.?holding|no.?record|not.?found|empty",
    re.I)


# ----------------------------------------------------------------------
def mask(token: str) -> str:
    t = (token or "").strip()
    if len(t) <= 8:
        return "****"
    return f"****{t[-4:]}  ({len(t)} chars)"


def token_expiry(token: str) -> dict:
    """Dhan ka access token ek JWT hai. Uske payload se expiry padh lete hain.

    Signature verify NAHI karte -- wo Dhan ka kaam hai. Hum sirf 'exp' claim
    dekh kar bata dete hain ki token kab tak chalega, taaki expire hone se
    pehle pata chal jaaye.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {"jwt": False}
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {"jwt": False}

    exp = payload.get("exp")
    out = {"jwt": True, "client_id_in_token": str(payload.get("dhanClientId") or "") or None}
    if not exp:
        return out
    try:
        dt = datetime.fromtimestamp(int(exp), tz=timezone.utc).astimezone(IST)
    except (ValueError, OSError, OverflowError):
        return out
    now = datetime.now(IST)
    secs = (dt - now).total_seconds()
    out.update({
        "expires_at": dt.strftime("%d %b %Y, %I:%M %p"),
        "expired": secs <= 0,
        "days_left": round(secs / 86400, 1),
        "hours_left": round(secs / 3600, 1),
    })
    return out


# ----------------------------------------------------------------------
def _step(name: str, ok: bool | None, msg: str, detail: str = "") -> dict:
    return {"name": name, "ok": ok, "msg": msg, "detail": detail}


def verify(client_id: str, token: str, base_url: str,
           timeout: int = 12) -> dict:
    """Credentials ko step-by-step check karo.

    Har step ka apna pass/fail aata hai taaki UI mein ek-ek karke tick lage
    aur fail hone par pata chale ki THEEK kahan atka.
    """
    client_id = (client_id or "").strip()
    token = (token or "").strip()
    steps: list[dict] = []

    # ---- 1. shakl-surat -----------------------------------------------
    if not client_id or not token:
        steps.append(_step("Format", False, "Client ID aur token dono chahiye."))
        return {"ok": False, "steps": steps}
    if not _CLIENT_RE.match(client_id):
        steps.append(_step("Format", False,
                           "Client ID sirf ank (digits) ka hota hai, 6-20 lamba.",
                           f"tumne diya: {len(client_id)} characters"))
        return {"ok": False, "steps": steps}
    steps.append(_step("Format", True, f"Client ID {client_id} theek dikhta hai."))

    # ---- 2. token expiry ----------------------------------------------
    info = token_expiry(token)
    if not info.get("jwt"):
        steps.append(_step("Token", None,
                           "Token JWT format mein nahi hai -- expiry nahi padh paya. "
                           "Aage badh kar asli API se check karte hain."))
    elif info.get("expired"):
        steps.append(_step("Token", False,
                           f"Token EXPIRE ho chuka hai ({info.get('expires_at')}).",
                           "Dhan par jaakar naya token banao."))
        return {"ok": False, "steps": steps, "token_info": info}
    else:
        d = info.get("days_left")
        tid = info.get("client_id_in_token")
        if tid and tid != client_id:
            steps.append(_step("Token", False,
                               f"Token kisi aur account ka hai (token mein {tid}, "
                               f"tumne {client_id} likha).",
                               "Dono ek hi Dhan account ke hone chahiye."))
            return {"ok": False, "steps": steps, "token_info": info}
        when = info.get("expires_at", "")
        if d is not None and d < 3:
            steps.append(_step("Token", None,
                               f"Token sirf {info.get('hours_left')} ghante mein "
                               f"expire ho jaayega ({when}).",
                               "Abhi chalega, par jaldi naya bana lena."))
        else:
            steps.append(_step("Token", True,
                               f"Token valid hai — {d} din baaki ({when})."))

    # ---- 3. asli API call: funds --------------------------------------
    s = requests.Session()
    s.headers.update({"access-token": token, "client-id": client_id,
                      "Accept": "application/json",
                      "Content-Type": "application/json"})
    base = base_url.rstrip("/")
    cash = None
    try:
        r = s.get(f"{base}/fundlimit", timeout=timeout)
    except requests.RequestException as e:
        steps.append(_step("Dhan se baat", False,
                           "Dhan API tak pahunch nahi paye.",
                           f"Internet chal raha hai? ({type(e).__name__})"))
        return {"ok": False, "steps": steps, "token_info": info}

    if r.status_code in (401, 403):
        steps.append(_step("Dhan se baat", False,
                           "Dhan ne credentials REJECT kar diye "
                           f"(HTTP {r.status_code}).",
                           "Token galat, expire, ya us account ka nahi hai."))
        return {"ok": False, "steps": steps, "token_info": info}
    if r.status_code >= 400:
        steps.append(_step("Dhan se baat", False,
                           f"Dhan ne HTTP {r.status_code} diya.",
                           (r.text or "")[:180]))
        return {"ok": False, "steps": steps, "token_info": info}

    try:
        f = r.json() or {}
    except ValueError:
        f = {}
    for k in ("availabelBalance", "availableBalance", "withdrawableBalance"):
        if k in f:
            cash = float(f[k] or 0)
            break
    steps.append(_step("Dhan se baat", True, "Credentials sahi hain — Dhan ne maan liya.",
                       f"Free cash: Rs.{cash:,.2f}" if cash is not None else ""))

    # ---- 4. data access -----------------------------------------------
    n_hold = None
    try:
        r2 = s.get(f"{base}/holdings", timeout=timeout)
        raw = (r2.text or "")[:300]
        if r2.status_code < 400:
            body = r2.json()
            if isinstance(body, dict):
                body = body.get("data") or body.get("holdings") or []
            n_hold = len(body) if isinstance(body, list) else 0
            steps.append(_step(
                "Portfolio", True,
                f"Holdings padh liye -- {n_hold} scrip mile." if n_hold else
                "Holdings padh liye -- abhi portfolio khaali hai (0 scrip). "
                "Ye theek hai; pehla deploy poore cash se hoga."))
        elif r2.status_code in (401, 403):
            steps.append(_step("Portfolio", False,
                               "Funds toh dikh gaye par holdings nahi.",
                               f"Dhan par Data API subscription check karo. "
                               f"Dhan ne kaha: {raw}"))
        elif _NO_DATA_RE.search(raw):
            n_hold = 0
            steps.append(_step(
                "Portfolio", True,
                "Portfolio khaali hai -- Dhan ne 'no data' bheja.",
                "Ye error nahi hai, naye account par aisa hi aata hai."))
        else:
            steps.append(_step(
                "Portfolio", False,
                f"Holdings NAHI mile -- Dhan ne HTTP {r2.status_code} diya.",
                f"Dhan ne kaha: {raw}"))
    except requests.RequestException as e:
        steps.append(_step("Portfolio", False,
                           "Holdings check nahi kar paye.", str(e)[:160]))

    ok = all(st["ok"] is not False for st in steps)
    return {"ok": ok, "steps": steps, "token_info": info,
            "cash": cash, "holdings": n_hold,
            "client_id": client_id, "masked": mask(token)}


# ----------------------------------------------------------------------
def save(root: Path, client_id: str, token: str,
         id_env: str, token_env: str) -> Path:
    """creds.bat likho -- wahi file jo START-APP.bat call karti hai."""
    f = Path(root) / CREDS_FILE
    f.write_text(
        "@echo off\r\n"
        "REM  Dhan credentials. Ye file kisi ko mat bhejna.\r\n"
        "REM  App ke UI se bani hai -- Connection tab se badal sakte ho.\r\n"
        f"set {id_env}={client_id}\r\n"
        f"set {token_env}={token}\r\n",
        encoding="utf-8")
    return f


def read_saved(root: Path, id_env: str, token_env: str) -> tuple[str, str]:
    """creds.bat se values wapas padho (agar hai)."""
    f = Path(root) / CREDS_FILE
    if not f.exists():
        return "", ""
    cid = tok = ""
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*set\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        if m.group(1) == id_env:
            cid = m.group(2)
        elif m.group(1) == token_env:
            tok = m.group(2)
    return cid, tok


def clear(root: Path) -> bool:
    """creds.bat ko _to_delete mein hata do (delete nahi karte -- wapas
    chahiye ho toh mil jaaye)."""
    f = Path(root) / CREDS_FILE
    if not f.exists():
        return False
    bak = Path(root) / f"{CREDS_FILE}.removed"
    f.replace(bak)
    return True
