#!/usr/bin/env python3
"""
cec.lt stock watcher - alerts when a product flips to "Į krepšelį".

The site is server-rendered behind Cloudflare; plain requests +
BeautifulSoup is sufficient. No browser, no Playwright.

Usage (Windows uses the `py` launcher):
    py stock_watch.py --check        # one fetch, full diagnostics
    py stock_watch.py --test-alert   # push a test alert to every channel
    py stock_watch.py --once         # single check: 0=in stock, 1=not, 2=error
    py stock_watch.py                # continuous loop

Alert channels (all optional; absent env vars degrade gracefully):
    NTFY_TOPIC            your ntfy.sh topic name    (primary, iPhone)
    TELEGRAM_BOT_TOKEN    e.g. 123456:ABC-DEF        (backup)
    TELEGRAM_CHAT_ID      e.g. 987654321
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Windows consoles default to cp1252 and raise UnicodeEncodeError on the
# Lithuanian text and on the non-breaking space in "1 459,00 €". Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PRODUCT_URL = ("https://www.cec.lt/mac/macbook-air-m5/"
               "macbook-air-13-m5-10-core-cpu-8-core-gpu-16gb-512gb")
CATEGORY_URL = "https://www.cec.lt/mac/macbook-air-m5"

# Buy-box phrases, diacritics-folded. Verified present on the live pages.
BUYABLE = ["i krepseli"]
OUT_OF_STOCK = ["prekes siuo metu neturime", "informuoti"]
PREORDER = ["registracija isigijimui", "registruotis"]

# CSS-module class prefixes. The hash suffix (e.g. __2EFR-) changes whenever
# the site is rebuilt, so match the stable prefix only and always keep a
# whole-page fallback.
BUYBOX_CLASS = re.compile(r"Product_add_to_cart_wrapper__")
STOCKS_CLASS = re.compile(r"Stocks_stores_wrapper__")
CITY_BLOCK_CLASS = re.compile(r"Stocks_stores_city__")
CITY_NAME_CLASS = re.compile(r"Stocks_city__")
STORE_CLASS = re.compile(r"Stocks_store__")

WAREHOUSE = "centrinis sandelis"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

STATE_FILE = Path(os.environ.get("STOCK_WATCH_STATE",
                                 Path.home() / ".stock_watch_state.json"))


def fold(text):
    """Lowercase, strip Lithuanian diacritics, collapse whitespace.

    NFKD also maps the non-breaking space in "1 459,00 €" to a plain space.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------

def fetch(url, timeout=30):
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": UA,
        "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    r.raise_for_status()
    return r.text


def _visible_text(node):
    for tag in node(["script", "style", "noscript"]):
        tag.decompose()
    return node.get_text(" ", strip=True)


def parse_variants(soup):
    """Per-SKU availability from JSON-LD, when the page carries it.

    On the target product page all 9 colour/keyboard SKUs appear here with
    their own availability, which is what lets a single fetch cover every
    colour. Many pages (accessories, custom configs) omit availability
    entirely, so an empty list means "no signal", never "out of stock".
    """
    variants = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for block in (data if isinstance(data, list) else [data]):
            if not isinstance(block, dict):
                continue
            if block.get("@type") not in ("Product", "ProductGroup"):
                continue
            for v in block.get("hasVariant") or [block]:
                if not isinstance(v, dict):
                    continue
                offers = v.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                avail = (offers.get("availability") or "").rsplit("/", 1)[-1]
                if not avail:
                    continue
                variants.append({
                    "sku": v.get("sku"),
                    "name": v.get("name"),
                    "color": v.get("color"),
                    "availability": avail,
                    "price": offers.get("price"),
                })
    return variants


def variant_label(v):
    """'Midnight / US' from a JSON-LD variant name.

    Names end in .../<colour>/<keyboard layout>, e.g.
    ".../16GB/512GB/Sky Blue/INT". Every colour and every layout counts as
    a hit, so this is only for telling you which one to pick.
    """
    name = v.get("name") or ""
    parts = [p.strip() for p in name.split("/") if p.strip()]
    if len(parts) >= 2:
        colour, layout = parts[-2], parts[-1]
        if len(layout) <= 12:
            return f"{colour} / {layout}"
    return v.get("color") or v.get("sku") or "unknown variant"


def parse_stores(soup):
    """Branch availability as [(city, [stores...])].

    Present only on in-stock pages. The warehouse entry has an empty city.
    """
    wrap = soup.find(class_=STOCKS_CLASS)
    if not wrap:
        return []
    out = []
    for block in wrap.find_all(class_=CITY_BLOCK_CLASS):
        city_el = block.find(class_=CITY_NAME_CLASS)
        city = city_el.get_text(" ", strip=True) if city_el else ""
        stores = [s.get_text(" ", strip=True)
                  for s in block.find_all(class_=STORE_CLASS)]
        stores = [s for s in stores if s]
        if city or stores:
            out.append((city, stores))
    return out


def parse_price(text):
    # The thousands separator is a non-breaking space: "Kaina 1 459,00 €".
    m = re.search(r"Kaina\s*([0-9  .,]+\s*€)", text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


class Snapshot:
    """Everything a single fetch tells us about a product page."""

    def __init__(self, html, url=PRODUCT_URL):
        self.url = url
        soup = BeautifulSoup(html, "html.parser")
        # JSON-LD must be read before scripts are stripped.
        self.variants = parse_variants(soup)
        self.stores = parse_stores(soup)
        box = soup.find(class_=BUYBOX_CLASS)
        self.scoped = box is not None
        # Scope matching to the buy box so an in-stock item in the "Susiję
        # produktai" carousel below it cannot be read as our product.
        self.buybox_text = _visible_text(box) if box else ""
        self.page_text = _visible_text(soup)
        self.match_text = self.buybox_text if self.scoped else self.page_text
        self.price = parse_price(self.match_text) or parse_price(self.page_text)
        folded = fold(self.match_text)
        self.matched = {
            "buyable": [p for p in BUYABLE if p in folded],
            "out_of_stock": [p for p in OUT_OF_STOCK if p in folded],
            "preorder": [p for p in PREORDER if p in folded],
        }
        self.status, self.conflict = self._classify()

    def _classify(self):
        m = self.matched
        buyable = bool(m["buyable"])
        oos = bool(m["out_of_stock"])
        pre = bool(m["preorder"])

        # Per-SKU availability outranks the buy-box text. The page renders
        # only the default variant (Sky Blue / standard layout), so if any
        # other colour or keyboard layout is back in stock the text alone
        # would still read "out of stock" and we would miss it. Every
        # colour and every layout is acceptable, so any hit is a hit.
        if self.in_stock_variants:
            return "in_stock", False

        if buyable and (oos or pre):
            # Ambiguous even after scoping to the buy box. A missed restock
            # costs far more than a false alarm, so alert - but say so loudly.
            return "in_stock", True
        if buyable:
            return "in_stock", False
        if pre:
            return "preorder_registration", False
        if oos:
            return "out_of_stock", False

        # No buy-box phrase at all. If JSON-LD says every SKU is out of stock
        # we can still trust that; otherwise we genuinely do not know.
        if self.variants and all(v["availability"] == "OutOfStock"
                                 for v in self.variants):
            return "out_of_stock", False
        return "unknown", False

    @property
    def in_stock_variants(self):
        return [v for v in self.variants if v["availability"] == "InStock"]

    def store_summary(self):
        """Human-readable branch list, walk-in cities first."""
        if not self.stores:
            return ""
        walk_in, warehouse = [], []
        for city, stores in self.stores:
            if not city and stores and fold(stores[0]) == WAREHOUSE:
                warehouse.append(stores[0])
            elif city:
                walk_in.append(f"{city}: {', '.join(stores)}" if stores else city)
            else:
                walk_in.extend(stores)
        if not walk_in and warehouse:
            return "Warehouse only (no store pickup): " + ", ".join(warehouse)
        parts = []
        if walk_in:
            parts.append("Walk in and buy - " + " | ".join(walk_in))
        if warehouse:
            parts.append("Also: " + ", ".join(warehouse))
        return "\n".join(parts)

    def has_vilnius(self):
        return any(fold(c).startswith("vilnius") for c, _ in self.stores)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def _ntfy(title, body, url, priority, tags):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return None
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        "Tags": tags,
        "Click": url,
    }
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"  ntfy FAILED: {e}")
        return False


def _telegram(title, body, url):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          timeout=15,
                          json={"chat_id": chat,
                                "text": f"{title}\n\n{body}\n\n{url}"})
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"  telegram FAILED: {e}")
        return False


def _local(title, body, urgent):
    """Best-effort Windows beep + balloon tip. Never fatal, never blocking."""
    try:
        if sys.platform == "win32":
            import winsound
            for _ in range(3 if urgent else 1):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.2)
        else:
            for _ in range(3 if urgent else 1):
                print("\a", end="", flush=True)
                time.sleep(0.2)
    except Exception:
        pass

    if sys.platform != "win32" or os.environ.get("STOCK_WATCH_NO_TOAST"):
        return
    try:
        safe_t = title.replace("'", "''")
        safe_b = body.replace("'", "''").replace("\n", " ")[:200]
        ps = (
            "[void][reflection.assembly]::LoadWithPartialName("
            "'System.Windows.Forms');"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            "$n.ShowBalloonTip(10000,'" + safe_t + "','" + safe_b + "',"
            "[System.Windows.Forms.ToolTipIcon]::Warning);"
            "Start-Sleep -Seconds 6;$n.Dispose()"
        )
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive",
                          "-WindowStyle", "Hidden", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def notify(title, body, url, urgent=True, tags="computer"):
    priority = 5 if urgent else 3
    log(f"NOTIFY [priority {priority}] {title}")
    results = {"ntfy": _ntfy(title, body, url, priority, tags),
               "telegram": _telegram(title, body, url)}
    _local(title, body, urgent)
    configured = {k: v for k, v in results.items() if v is not None}
    if not configured:
        log("  WARNING: no push channel configured - set NTFY_TOPIC")
    else:
        log("  channels: " + ", ".join(
            f"{k}={'ok' if v else 'FAILED'}" for k, v in configured.items()))
    return results


DEFAULT_LABEL = "MacBook Air M5 16/512"


def build_alert(snap, label=DEFAULT_LABEL):
    """Title and body for an in-stock alert."""
    where = snap.store_summary()
    prefix = "IN STOCK IN VILNIUS" if snap.has_vilnius() else "IN STOCK"
    title = f"{prefix} - {label}"

    lines = ["Add to cart is live. Go buy it."]
    if snap.price:
        lines.append(f"Price: {snap.price}")
    hits = snap.in_stock_variants
    if hits:
        combos = sorted({variant_label(v) for v in hits})
        lines.append(f"Available now ({len(combos)}):")
        lines.extend(f"  - {c}" for c in combos)
        if not snap.matched["buyable"]:
            lines.append("The page opens on a variant that is still out of "
                         "stock - use the colour and keyboard selectors to "
                         "pick one of the above.")
    if where:
        lines.append(where)
    if snap.conflict:
        lines.append("NOTE: page showed both in-stock and out-of-stock text. "
                     "Alerting anyway - verify before celebrating.")
    if not snap.scoped:
        lines.append("NOTE: buy-box container not found; matched whole page. "
                     "Markup may have changed.")
    return title, "\n".join(lines)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"could not write state file: {e}")


# --------------------------------------------------------------------------
# Category page (new-SKU risk, brief 3.2)
# --------------------------------------------------------------------------

def category_tiles(html):
    """Product paths on the category page that look like 16GB + 512GB."""
    soup = BeautifulSoup(html, "html.parser")
    paths = set()
    for a in soup.find_all("a", href=True):
        h = a["href"].split("?")[0].rstrip("/")
        if "/mac/macbook-air-m5/" in h:
            paths.add(h)
    folded = {p: fold(p) for p in paths}
    matching = {p for p, f in folded.items() if "16gb" in f and "512gb" in f}
    return sorted(paths), sorted(matching)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def do_check(url):
    """One fetch, maximum diagnostics. The debugging entry point."""
    print(f"Fetching {url}")
    html = fetch(url)
    print(f"HTML length: {len(html):,} bytes")
    snap = Snapshot(html, url)

    print("\n=== VERDICT ===")
    print(f"  status:        {snap.status}")
    print(f"  conflict:      {snap.conflict}")
    print(f"  price:         {snap.price}")
    print(f"  buy box found: {snap.scoped}"
          f"{'' if snap.scoped else '  <-- fell back to whole-page match'}")

    print("\n=== MATCHED PHRASES (folded) ===")
    for k, v in snap.matched.items():
        print(f"  {k:13} {v if v else '-'}")

    print(f"\n=== JSON-LD VARIANTS ({len(snap.variants)}) ===")
    if not snap.variants:
        print("  none (page carries no per-SKU availability; text match only)")
    for v in snap.variants:
        flag = ("IN STOCK  <<<" if v["availability"] == "InStock"
                else v["availability"])
        print(f"  {str(v['sku']):12} {variant_label(v):22} {flag}")
    if snap.variants:
        combos = sorted({variant_label(v) for v in snap.variants})
        print(f"  ({len(combos)} distinct colour/keyboard combinations "
              f"watched; any one of them triggers an alert)")

    print("\n=== BRANCHES ===")
    if snap.stores:
        for city, stores in snap.stores:
            print(f"  {city or '(warehouse)':14} {', '.join(stores)}")
        print(f"  summary: {snap.store_summary()}")
        print(f"  vilnius: {snap.has_vilnius()}")
    else:
        print("  none listed (expected while out of stock)")

    print("\n=== RAW BUY-BOX TEXT ===")
    print(snap.buybox_text[:700] if snap.buybox_text
          else snap.page_text[:700])
    return snap


def check_category():
    """Report tiles on the category page; flag any new 16/512 URL."""
    html = fetch(CATEGORY_URL)
    all_paths, matching = category_tiles(html)
    return all_paths, matching


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_once(args, state):
    """One poll cycle. Returns (status, snapshot). Mutates and saves state."""
    snap = Snapshot(fetch(args.url), args.url)
    entry = state.setdefault(args.url, {})
    prev_status = entry.get("status")
    prev_price = entry.get("price")
    last_alert = entry.get("last_alert", 0)
    unknown_streak = entry.get("unknown_streak", 0)
    now = time.time()

    # Per-SKU coverage guard. Every colour and keyboard layout is watched via
    # the page's JSON-LD; the rendered text only ever reflects the default
    # variant. If that block vanishes in a site rebuild we silently drop to
    # watching one combination out of seven - which looks perfectly healthy
    # while missing a Midnight or US-layout restock entirely.
    prev_variants = entry.get("variant_count")
    cur_variants = len(snap.variants)
    if prev_variants and not cur_variants:
        log(f"  !! per-SKU coverage lost ({prev_variants} SKUs -> 0)")
        notify("Watcher lost per-SKU coverage",
               f"This page used to expose {prev_variants} SKUs with their own "
               "availability; it now exposes none. The watcher has fallen "
               "back to reading only the default variant, so a restock in "
               "another colour or keyboard layout could be missed.\n\n"
               "Run: py stock_watch.py --check",
               args.url, urgent=False, tags="warning")
    entry["variant_count"] = cur_variants

    if snap.status == "unknown":
        unknown_streak += 1
        log(f"status: unknown ({unknown_streak}x) - "
            f"no buy-box phrase matched; markup may have changed")
        if unknown_streak == 3:
            notify("Watcher needs attention",
                   "Three checks in a row found no known stock phrase on the "
                   "page. The markup may have changed, or the fetch is being "
                   "blocked. Check the page manually.",
                   args.url, urgent=False, tags="warning")
    else:
        unknown_streak = 0
        where = snap.store_summary()
        detail = f"status: {snap.status}"
        if snap.price:
            detail += f"  ({snap.price})"
        if snap.in_stock_variants:
            detail += f"  [{len(snap.in_stock_variants)} SKU in stock]"
        log(detail + (f"\n           {where}" if where else ""))

        if snap.conflict:
            log("  !! CONFLICT: buy box contained both in-stock and "
                "out-of-stock phrases - treating as IN STOCK")

        if snap.status == "in_stock":
            due = (prev_status != "in_stock"
                   or now - last_alert > args.repeat_alert_min * 60)
            if due:
                title, body = build_alert(snap, args.label)
                notify(title, body, args.url, urgent=True)
                last_alert = now
            else:
                wait = int(args.repeat_alert_min * 60 - (now - last_alert))
                log(f"  (already alerted; next repeat in ~{wait}s)")

        elif snap.status == "preorder_registration" and prev_status != snap.status:
            notify(f"Pre-order registration open - {args.label}",
                   "The page moved to the registration state "
                   "(Registracija įsigijimui). Not a normal add-to-cart, "
                   "but worth a look.",
                   args.url, urgent=False, tags="calendar")

        if prev_price and snap.price and snap.price != prev_price:
            notify(f"Price changed - {args.label}",
                   f"{prev_price}  ->  {snap.price}",
                   args.url, urgent=False, tags="moneybag")

        entry["price"] = snap.price or prev_price
        entry["status"] = snap.status

    entry["unknown_streak"] = unknown_streak
    entry["checked_at"] = datetime.now().isoformat(timespec="seconds")
    entry["last_alert"] = last_alert
    if not args.no_state:
        save_state(state)
    return snap


def check_category_once(state, args):
    """Lower-priority watch for a new 16/512 tile appearing (brief 3.2)."""
    try:
        all_paths, matching = check_category()
    except Exception as e:
        log(f"category check failed: {e}")
        return
    entry = state.setdefault(CATEGORY_URL, {})
    known = set(entry.get("matching", []))
    new = [p for p in matching if p not in known and p not in args.url]
    if known and new:
        notify("New 16GB/512GB listing on cec.lt",
               "A tile matching 16GB + 512GB appeared that the watcher had "
               "not seen before:\n" + "\n".join(new) +
               "\n\nThe shipment may have landed under a new URL.",
               CATEGORY_URL, urgent=False, tags="mag")
    entry["matching"] = matching
    entry["all_count"] = len(all_paths)
    if not args.no_state:
        save_state(state)


def main():
    ap = argparse.ArgumentParser(
        description="Watch cec.lt for a product coming back into stock.")
    ap.add_argument("--url", default=PRODUCT_URL)
    ap.add_argument("--label", default=DEFAULT_LABEL,
                    help="product name used in alert titles")
    ap.add_argument("--interval", type=int, default=90,
                    help="seconds between polls in loop mode (default 90)")
    ap.add_argument("--once", action="store_true",
                    help="single check; exit 0=in stock, 1=not, 2=error")
    ap.add_argument("--check", action="store_true",
                    help="one fetch with full diagnostics")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert through every configured channel")
    ap.add_argument("--repeat-alert-min", type=int, default=10,
                    help="re-fire the in-stock alert every N minutes")
    ap.add_argument("--no-state", action="store_true",
                    help="do not persist state (alerts every run)")
    ap.add_argument("--duration", type=int, default=0,
                    help="with --once, keep polling for this many seconds "
                         "instead of checking a single instant; returns as "
                         "soon as it is in stock. Lets one CI run cover a "
                         "window, so irregular cron timing matters less.")
    ap.add_argument("--watch-category", action="store_true",
                    help="also watch the category page for a new 16/512 tile")
    args = ap.parse_args()

    if args.test_alert:
        results = notify(
            "TEST - cec.lt watcher",
            "If you can read this on your phone lock screen, alerts work.\n"
            "Tapping this notification should open the product page.",
            args.url, urgent=True)
        pushed = [k for k, v in results.items() if v]
        if pushed:
            log(f"test PUSH sent via: {', '.join(pushed)}")
            log("Check your phone. If nothing arrives, see README section 4.1.")
            return 0
        log("NO PUSH WAS SENT - only the local Windows notification fired.")
        log("Your phone will NOT be alerted when stock lands.")
        log("Fix: subscribe to a topic in the ntfy iOS app, then set it here:")
        log('  $env:NTFY_TOPIC = "<your-topic>"')
        return 1

    if args.check:
        try:
            do_check(args.url)
            if args.watch_category:
                all_paths, matching = check_category()
                print(f"\n=== CATEGORY TILES ({len(all_paths)}) ===")
                for p in all_paths:
                    mark = " <-- 16/512" if p in matching else ""
                    print(f"  {p}{mark}")
        except Exception as e:
            log(f"check failed: {type(e).__name__}: {e}")
            return 2
        return 0

    state = {} if args.no_state else load_state()

    if args.once:
        deadline = time.time() + args.duration
        code, first = 1, True
        while True:
            try:
                snap = run_once(args, state)
                if first and args.watch_category:
                    check_category_once(state, args)
                if snap.status == "in_stock":
                    return 0
                code = 1
            except Exception as e:
                log(f"error: {type(e).__name__}: {e}")
                code = 2
            first = False
            # Stop once another full poll would overrun the budget, so the
            # job never exceeds the duration it was given.
            if time.time() + args.interval >= deadline:
                return code
            time.sleep(args.interval + random.uniform(-5, 5))

    log(f"watching {args.url}")
    log(f"polling every ~{args.interval}s with jitter (Ctrl-C to stop)")
    if not os.environ.get("NTFY_TOPIC"):
        log("WARNING: NTFY_TOPIC is not set - no phone push will be sent")

    failures = 0
    last_category_check = 0.0
    try:
        while True:
            try:
                run_once(args, state)
                failures = 0
                if args.watch_category and time.time() - last_category_check > 900:
                    check_category_once(state, args)
                    last_category_check = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                failures += 1
                log(f"fetch error ({failures}): {type(e).__name__}: {e}")
                if failures == 5:
                    notify("Watcher is failing",
                           f"5 consecutive errors talking to cec.lt.\n"
                           f"Last error: {e}",
                           args.url, urgent=False, tags="warning")
                backoff = min(900, args.interval * (2 ** min(failures - 1, 4)))
                log(f"backing off {backoff}s")
                time.sleep(backoff)
                continue
            time.sleep(max(20, args.interval + random.uniform(-10, 20)))
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
