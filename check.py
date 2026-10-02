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
from datetime import datetime, timezone
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


_browser = None


def fetch_json_browser(url: str) -> dict | None:
    """Fallback: run the fetch inside a real (headless) Chromium page."""
    global _browser
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed; skipping browser fallback", file=sys.stderr)
        return None
    if _browser is None:
        pw = sync_playwright().start()
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()
        page.goto(f"{BASE}/shop/buy-iphone", wait_until="domcontentloaded", timeout=60000)
        _browser = page
    res = _browser.evaluate(
        """async (u) => { const r = await fetch(u, {headers:{Accept:'application/json'}});
                          return {status: r.status, body: await r.text()}; }""",
        url,
    )
    if res["status"] == 200:
        try:
            return json.loads(res["body"])
        except ValueError:
            pass
    print(f"  browser fetch failed (HTTP {res['status']})", file=sys.stderr)
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
def send_email(subject: str, body: str) -> None:
    host, user, pw, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if not all((host, user, pw, to)):
        return
    msg = MIMEText(body, "plain", "utf-8")
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


def notify(subject: str, body: str) -> None:
    for fn, args in ((send_email, (subject, body)),
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
    for z in zips:
        for cp, parts in by_carrier.items():
            url = build_url(parts, z, cp)
            print(f"Checking ZIP {z} [{cp}] ...")
            data = fetch_json_requests(session, url) or fetch_json_browser(url)
            if data is None:
                errors.append(f"Apple blocked/failed lookup for ZIP {z} ({cp})")
                continue
            for r in parse_stores(data, parts, radius):
                r["zip"] = z
                rows.append(r)
            time.sleep(2)  # be polite

    available = [r for r in rows if r["available"]]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"Checked at {now} | ZIPs: {', '.join(zips)} | radius {radius:g} mi", ""]
    for part, label in labels.items():
        lines.append(f"== {label} ({part})")
        hits = [r for r in rows if r["part"] == part]
        if not hits:
            lines.append("   No Apple Store within radius returned data.")
        for r in sorted(hits, key=lambda x: (not x["available"], x["distance"] or 999)):
            mark = "AVAILABLE" if r["available"] else "not available"
            dist = f"{r['distance']:.1f} mi" if r["distance"] is not None else "? mi"
            lines.append(f"   [{mark}] {r['store']}, {r['city']} {r['state']} - {dist} "
                         f"(ZIP {r['zip']}) {r['quote']}".rstrip())
        lines.append("")
    if errors:
        lines += ["Errors:"] + [f"   - {e}" for e in errors]
    lines.append("Buy: https://www.apple.com/shop/buy-iphone")
    body = "\n".join(lines)

    if available:
        uniq = {(r["part"], r["store"]) for r in available}
        subject = f"✅ iPhone AVAILABLE for pickup ({len(uniq)} store/config match)"
    elif errors and not rows:
        subject = "⚠️ iPhone availability check FAILED"
    else:
        subject = "❌ iPhone not available near you"

    print("\n" + subject + "\n" + body)
    if not dry_run:
        notify(subject, body)
    return 1 if (errors and not rows) else 0


def list_configs(model: str) -> None:
    for p in sorted(fetch_catalog(requests.Session(), model), key=lambda p: p["name"]):
        print(f"{p['partNumber']:12} {p['name']}")


def main() -> None:
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
