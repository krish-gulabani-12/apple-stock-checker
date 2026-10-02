#!/usr/bin/env python3
"""
Apple Store (US) in-store pickup availability checker.

Reads config.json, resolves each configured iPhone to an Apple part number,
queries Apple's pickup availability for every ZIP code, keeps stores within
the configured radius, and sends a notification on every run (available or not).

Usage:
  python check.py                      # run a check and notify
  python check.py --dry-run            # run a check, print only (no notifications)
  python check.py --list "iPhone 18 Pro"  # list valid colour/storage combos + part numbers

Notification channels are enabled by environment variables (GitHub Secrets):
  Email (SMTP):   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO
  WhatsApp (CallMeBot, free): CALLMEBOT_PHONE, CALLMEBOT_APIKEY
  WhatsApp (Twilio):  TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = "https://www.apple.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Carrier "policy" codes Apple's US buy flow uses. The phone part number is the
# same across carriers; the carrier is passed separately.
CARRIERS = {
    "unlocked": "UNLOCKED/US",
    "att": "ATT/US",
    "at&t": "ATT/US",
    "verizon": "VERIZON/US",
    "tmobile": "TMOBILE/US",
    "t-mobile": "TMOBILE/US",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


# --------------------------------------------------------------------------- #
# Part number resolution (model + colour + storage -> part number)
# --------------------------------------------------------------------------- #
def slug_candidates(model: str) -> list[str]:
    """'iPhone 18 Pro Max' -> ['iphone-18-pro-max', 'iphone-18-pro']"""
    words = norm(model).split(" ")
    cands = ["-".join(words)]
    if words[-1] in ("max", "plus", "mini"):
        cands.append("-".join(words[:-1]))
    return cands


_catalog_cache: dict[str, list[dict]] = {}


def fetch_catalog(session: requests.Session, model: str) -> list[dict]:
    """Return [{'partNumber','name'}...] from the model's buy page."""
    for slug in slug_candidates(model):
        if slug in _catalog_cache:
            return _catalog_cache[slug]
        r = session.get(f"{BASE}/shop/buy-iphone/{slug}", headers=HEADERS, timeout=30)
        if r.status_code != 200:
            continue
        m = re.search(r'<script[^>]*id="metrics"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            continue
        try:
            products = json.loads(m.group(1))["data"]["products"]
        except (KeyError, ValueError):
            continue
        products = [p for p in products if p.get("partNumber") and p.get("name")]
        if products:
            _catalog_cache[slug] = products
            return products
    raise RuntimeError(f"Could not load Apple catalog for model '{model}'")


def resolve_part(session: requests.Session, dev: dict) -> str:
    if dev.get("part"):
        return dev["part"]
    wanted = norm(f"{dev['model']} {dev['storage']} {dev['color']}")
    for p in fetch_catalog(session, dev["model"]):
        if norm(p["name"]) == wanted:
            return p["partNumber"]
    raise RuntimeError(
        f"No match for '{dev['model']} {dev['storage']} {dev['color']}'. "
        f"Run: python check.py --list \"{dev['model']}\""
    )


# --------------------------------------------------------------------------- #
# Availability lookup
# --------------------------------------------------------------------------- #
def build_url(parts: list[str], zip_code: str, cppart: str | None) -> str:
    q = {"fae": "true", "pl": "true", "mts.0": "regular", "mts.1": "compact",
         "location": zip_code, "searchNearby": "true"}
    if cppart:
        q["cppart"] = cppart
    for i, p in enumerate(parts):
        q[f"parts.{i}"] = p
    return f"{BASE}/shop/fulfillment-messages?{urlencode(q)}"


def fetch_json_requests(session: requests.Session, url: str) -> dict | None:
    # Warm up cookies the way a browser would.
    session.get(f"{BASE}/shop/buy-iphone", headers=HEADERS, timeout=30)
    r = session.get(url, headers={**HEADERS, "Accept": "application/json",
                                  "Referer": f"{BASE}/shop/buy-iphone"}, timeout=30)
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    print(f"  plain HTTP blocked/failed (HTTP {r.status_code})", file=sys.stderr)
    return None


_pw = None
_browser = None     # Browser object
_page = None        # page with Apple cookies, reused across lookups
_mode = None        # "headless" or "windowed"


def _open_browser(headless: bool):
    """Launch Chrome and load Apple's store page so its scripts set cookies."""
    global _pw
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    # Apple rejects (HTTP 541) browsers that expose navigator.webdriver.
    args = ["--disable-blink-features=AutomationControlled"]
    if not headless:
        args.append("--window-position=-32000,-32000")  # real window, off-screen
    kw = {"headless": headless, "args": args}
    try:
        b = _pw.chromium.launch(channel="chrome", **kw)   # real Google Chrome
    except Exception:
        b = _pw.chromium.launch(**kw)                     # Playwright's Chromium
    ctx_kw = {"locale": "en-US"}
    if headless:  # use the browser's own UA, minus "Headless"
        probe = b.new_page()
        ctx_kw["user_agent"] = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        probe.close()
    page = b.new_context(**ctx_kw).new_page()
    r = page.goto(f"{BASE}/shop/buy-iphone", wait_until="load", timeout=60000)
    page.wait_for_timeout(5000)  # let Apple's bot-check scripts finish
    mode = "headless" if headless else "windowed"
    print(f"  browser fallback v3 ({mode}): Chrome {b.version}, store page HTTP "
          f"{r.status if r else '?'}", file=sys.stderr)
    return b, page, mode


def _page_fetch(page, url: str) -> dict:
    """Apple's bot check needs a few seconds after page load before it lets
    requests through (they get 541 until then), so retry with waits and
    reload the store page once if still blocked."""
    res = {"status": "?"}
    for attempt in range(6):
        res = page.evaluate(
            """async (u) => { const r = await fetch(u, {headers:{Accept:'application/json'}});
                              return {status: r.status, body: await r.text()}; }""",
            url,
        )
        if res["status"] == 200:
            return res
        if attempt == 2:
            page.goto(f"{BASE}/shop/buy-iphone", wait_until="load", timeout=60000)
        page.wait_for_timeout(4000)
    return res


def fetch_json_browser(url: str) -> dict | None:
    """Fallback: fetch inside a real Chrome page. Tries a normal Chrome window
    (placed off-screen) first, which Apple accepts most reliably, then headless.
    Force a mode with env BROWSER_MODE=headless or BROWSER_MODE=windowed."""
    global _browser, _page, _mode
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("  playwright not installed; skipping browser fallback", file=sys.stderr)
        return None
    forced = os.getenv("BROWSER_MODE", "").strip().lower()
    modes = [forced] if forced in ("headless", "windowed") else ["windowed", "headless"]
    if _mode in modes:  # resume from the mode that last worked
        modes = modes[modes.index(_mode):]
    res = {"status": "?"}
    for mode in modes:
        if _mode != mode:
            if _browser is not None:
                _browser.close()
            _browser, _page, _mode = _open_browser(headless=(mode == "headless"))
        res = _page_fetch(_page, url)
        if res["status"] == 200:
            try:
                return json.loads(res["body"])
            except ValueError:
                pass
        print(f"  browser fetch failed ({mode}, HTTP {res['status']})", file=sys.stderr)
    return None


def parse_distance(store: dict) -> float | None:
    for k in ("storedistance", "storeDistance"):
        if isinstance(store.get(k), (int, float)):
            return float(store[k])
    txt = store.get("storeDistanceWithUnit") or store.get("storedistanceWithUnit") or ""
    m = re.search(r"([\d.]+)", str(txt))
    return float(m.group(1)) if m else None


def parse_stores(data: dict, parts: list[str], radius: float) -> list[dict]:
    """Return one row per (store, part) within radius."""
    stores = (data.get("body", {}).get("content", {})
              .get("pickupMessage", {}).get("stores", [])) or []
    rows = []
    for s in stores:
        dist = parse_distance(s)
        if dist is not None and dist > radius:
            continue
        addr = s.get("address", {}) or {}
        for part in parts:
            pa = (s.get("partsAvailability") or {}).get(part)
            if not pa:
                continue
            quote = (pa.get("pickupSearchQuote")
                     or pa.get("messageTypes", {}).get("regular", {}).get("storePickupQuote")
                     or "")
            rows.append({
                "part": part,
                "store": s.get("storeName", "?"),
                "city": s.get("city") or addr.get("city", ""),
                "state": s.get("state") or addr.get("state", ""),
                "distance": dist,
                "available": pa.get("pickupDisplay") == "available",
                "quote": re.sub(r"<[^>]+>", "", quote),
            })
    return rows


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def send_email(subject: str, body: str, html: str | None = None) -> None:
    host, user, pw, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if not all((host, user, pw, to)):
        return
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print("  email sent")


def send_callmebot(text: str) -> None:
    phone, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not (phone and key):
        return
    r = requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": phone, "text": text[:1500], "apikey": key}, timeout=30)
    print(f"  callmebot whatsapp: HTTP {r.status_code}")


def send_twilio(text: str) -> None:
    sid, tok, frm, to = (os.getenv(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO"))
    if not all((sid, tok, frm, to)):
        return
    r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                      auth=(sid, tok),
                      data={"From": f"whatsapp:{frm}", "To": f"whatsapp:{to}", "Body": text[:1500]},
                      timeout=30)
    print(f"  twilio whatsapp: HTTP {r.status_code}")


def notify(subject: str, body: str, html: str | None = None) -> None:
    for fn, args in ((send_email, (subject, body, html)),
                     (send_callmebot, (f"{subject}\n\n{body}",)),
                     (send_twilio, (f"{subject}\n\n{body}",))):
        try:
            fn(*args)
        except Exception as e:  # one channel failing must not kill the others
            print(f"  {fn.__name__} failed: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(cfg: dict, dry_run: bool) -> int:
    session = requests.Session()
    radius = float(cfg.get("radius_miles", 20))
    zips = [str(z) for z in cfg["zips"]]

    # Resolve devices -> part numbers, grouped by carrier code.
    labels: dict[str, str] = {}
    by_carrier: dict[str | None, list[str]] = defaultdict(list)
    errors: list[str] = []
    for dev in cfg["devices"]:
        label = dev.get("label") or " ".join(
            str(dev[k]) for k in ("model", "storage", "color", "carrier") if dev.get(k))
        try:
            part = resolve_part(session, dev)
        except Exception as e:
            errors.append(str(e))
            continue
        carrier = dev.get("carrier", "unlocked")
        cp = CARRIERS.get(norm(carrier), carrier) if carrier else None
        labels[part] = label
        if part not in by_carrier[cp]:
            by_carrier[cp].append(part)
        print(f"{label} -> {part} ({cp})")

    rows: list[dict] = []
    failed: set[tuple[str, str]] = set()          # (zip, part) lookups that failed
    for z in zips:
        for cp, parts in by_carrier.items():
            url = build_url(parts, z, cp)
            print(f"Checking ZIP {z} [{cp}] ...")
            data = fetch_json_requests(session, url) or fetch_json_browser(url)
            if data is None:
                errors.append(f"Apple blocked/failed lookup for ZIP {z} ({cp})")
                failed.update((z, p) for p in parts)
                continue
            for r in parse_stores(data, parts, radius):
                r["zip"] = z
                rows.append(r)
            time.sleep(2)  # be polite

    summary = build_summary(labels, zips, rows, failed)
    n_avail = sum(1 for d in summary for z in d["zips"] if z["status"] == "available")
    all_failed = bool(errors) and not rows
    if n_avail:
        subject = f"✅ iPhone AVAILABLE near {n_avail} ZIP/model combo{'s' if n_avail > 1 else ''}"
    elif all_failed:
        subject = "⚠️ iPhone availability check FAILED"
    else:
        subject = "❌ iPhone not available near your ZIPs"

    checked_at = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    text = render_text(summary, checked_at, radius, errors)
    html = render_html(subject, summary, checked_at, radius, errors)
    print("\n" + subject + "\n" + text)
    if not dry_run:
        notify(subject, text, html)
    return 1 if all_failed else 0


# --------------------------------------------------------------------------- #
# Report formatting (one status per model per ZIP; only available stores listed)
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
BUY_URL = "https://www.apple.com/shop/buy-iphone"
MAX_STORES = 3  # nearest available stores listed per ZIP; the rest are counted


def build_summary(labels: dict, zips: list[str], rows: list[dict], failed: set) -> list[dict]:
    out = []
    for part, label in labels.items():
        zs = []
        for z in zips:
            hits = [r for r in rows if r["part"] == part and r["zip"] == z]
            stores = sorted((r for r in hits if r["available"]),
                            key=lambda r: r["distance"] if r["distance"] is not None else 999)
            if stores:
                status = "available"
            elif (z, part) in failed:
                status = "failed"
            elif hits:
                status = "unavailable"
            else:
                status = "nostores"
            zs.append({"zip": z, "status": status, "stores": stores})
        out.append({"label": label, "part": part, "zips": zs})
    return out


STATUS_TEXT = {
    "unavailable": "Not available",
    "failed": "Check failed (Apple blocked the lookup)",
    "nostores": "No Apple Store within radius",
}


def _store_line(r: dict) -> str:
    dist = f"{r['distance']:.1f} mi" if r["distance"] is not None else "? mi"
    return f"{r['store']}, {r['city']} {r['state']} ({dist})"


def render_text(summary: list[dict], checked_at: str, radius: float, errors: list[str]) -> str:
    lines = [f"Checked {checked_at} | within {radius:g} miles", ""]
    for d in summary:
        lines.append(d["label"])
        for z in d["zips"]:
            if z["status"] == "available":
                lines.append(f"  ZIP {z['zip']}: AVAILABLE at {len(z['stores'])} store(s)")
                lines += [f"    - {_store_line(r)}" for r in z["stores"][:MAX_STORES]]
                if len(z["stores"]) > MAX_STORES:
                    lines.append(f"    + {len(z['stores']) - MAX_STORES} more")
            else:
                lines.append(f"  ZIP {z['zip']}: {STATUS_TEXT[z['status']]}")
        lines.append("")
    if errors and not any(z["status"] == "failed" for d in summary for z in d["zips"]):
        lines += ["Notes:"] + [f"  - {e}" for e in errors] + [""]
    lines.append(f"Buy: {BUY_URL}")
    return "\n".join(lines)


def render_html(subject: str, summary: list[dict], checked_at: str, radius: float,
                errors: list[str]) -> str:
    from html import escape as e
    badge = {
        "available": ("#e6f4ea", "#137333", "Available"),
        "unavailable": ("#f1f3f4", "#5f6368", "Not available"),
        "failed": ("#fef7e0", "#b06000", "Check failed"),
        "nostores": ("#f1f3f4", "#5f6368", "No store nearby"),
    }
    cards = []
    for d in summary:
        rows_html = []
        for z in d["zips"]:
            bg, fg, txt = badge[z["status"]]
            if z["status"] == "available":
                txt = f"Available at {len(z['stores'])} store{'s' if len(z['stores']) > 1 else ''}"
            detail = ""
            if z["stores"]:
                detail = "".join(
                    f'<div style="font-size:13px;color:#3c4043;margin-top:4px">• {e(_store_line(r))}</div>'
                    for r in z["stores"][:MAX_STORES])
                if len(z["stores"]) > MAX_STORES:
                    detail += (f'<div style="font-size:13px;color:#80868b;margin-top:4px">'
                               f'+ {len(z["stores"]) - MAX_STORES} more</div>')
            rows_html.append(
                f'<tr><td style="padding:10px 12px;border-top:1px solid #eee;width:90px;'
                f'font-weight:600;color:#202124;vertical-align:top">ZIP {e(z["zip"])}</td>'
                f'<td style="padding:10px 12px;border-top:1px solid #eee">'
                f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;'
                f'background:{bg};color:{fg};font-size:13px;font-weight:600">{e(txt)}</span>'
                f'{detail}</td></tr>')
        cards.append(
            f'<div style="border:1px solid #dadce0;border-radius:10px;margin:0 0 16px;overflow:hidden">'
            f'<div style="padding:12px;background:#f8f9fa;font-weight:600;font-size:15px;color:#202124">'
            f'{e(d["label"])} <span style="color:#80868b;font-weight:400;font-size:12px">'
            f'{e(d["part"])}</span></div>'
            f'<table style="width:100%;border-collapse:collapse">{"".join(rows_html)}</table></div>')
    note = ""
    if errors and not any(z["status"] == "failed" for d in summary for z in d["zips"]):
        note = ('<div style="font-size:12px;color:#b06000;margin-top:8px">'
                + "<br>".join(e(x) for x in errors) + "</div>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        'max-width:600px;margin:0 auto;padding:16px">'
        f'<h2 style="margin:0 0 4px;font-size:20px;color:#202124">{e(subject)}</h2>'
        f'<div style="color:#5f6368;font-size:13px;margin-bottom:16px">'
        f'Checked {e(checked_at)} &middot; within {radius:g} miles</div>'
        f'{"".join(cards)}{note}'
        f'<a href="{BUY_URL}" style="display:inline-block;margin-top:8px;padding:10px 18px;'
        'background:#0071e3;color:#fff;text-decoration:none;border-radius:18px;font-size:14px">'
        'Open Apple Store</a></div>')


def list_configs(model: str) -> None:
    for p in sorted(fetch_catalog(requests.Session(), model), key=lambda p: p["name"]):
        print(f"{p['partNumber']:12} {p['name']}")


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a local .env file (for running on your own PC).
    Real environment variables (e.g. GitHub Secrets) take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def main() -> None:
    load_env_file(Path(__file__).with_name(".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", metavar="MODEL")
    a = ap.parse_args()
    if a.list:
        list_configs(a.list)
        return
    cfg = json.loads(Path(a.config).read_text())
    sys.exit(run(cfg, a.dry_run))


if __name__ == "__main__":
    main()
