# cec.lt stock watcher

Polls a [cec.lt](https://www.cec.lt) product page and sends a push notification
the moment it becomes buyable — including when only a non-default colour or
keyboard layout comes back in stock.

Built for a MacBook Air M5 restock, but works on any cec.lt product page.

```bash
pip install -r requirements.txt
python stock_watch.py --check
```

## How it decides

cec.lt renders three states in the buy box:

| State | Text | Meaning |
|---|---|---|
| Buyable | `Į krepšelį` | Add to cart |
| Out of stock | `Prekės šiuo metu neturime` + `Informuoti` | Notify-me |
| Registration | `Registracija įsigijimui` | Launch-window signup |

Matching is diacritics-insensitive (NFKD, combining marks stripped) and scoped
to the buy-box container, so an in-stock item in the related-products carousel
below it cannot cause a false positive.

### Per-SKU availability matters more than the text

Product pages embed a JSON-LD `ProductGroup` listing every colour and keyboard
variant with its own `offers.availability`. The **rendered buy box only ever
reflects the default variant** — so if a non-default colour restocks while the
default stays empty, the page text still reads "out of stock".

A text-only watcher misses that entirely. This one reads per-SKU availability
first:

```
any variant InStock          -> in_stock  (whatever the text says)
else buyable phrase          -> in_stock
else registration phrase     -> preorder_registration
else out-of-stock phrase     -> out_of_stock
else all variants OutOfStock -> out_of_stock
else                         -> unknown
```

When it fires on a non-default variant the alert names the exact combination
so you know which selector to change.

### Two traps worth knowing

**Availability is sometimes absent.** Many pages (accessories especially) omit
the field while genuinely being in stock. Absence is treated as *no signal*,
never as out-of-stock, with a fallback to text matching. If a page that
previously exposed per-SKU data stops doing so, the watcher warns — otherwise
it would silently drop to watching one variant out of several while still
reporting healthy.

**Category-page availability is boilerplate.** Listing pages report `InStock`
for every tile regardless of truth, contradicting the product pages they link
to. It is never used for stock state — only to spot a new product URL
appearing.

## Alerts

[ntfy](https://ntfy.sh) is the primary channel (free, no account, iOS/Android),
with Telegram as an optional independent backup. All channels are configured by
environment variable and degrade gracefully when unset.

```bash
export NTFY_TOPIC="your-topic-name"          # required for push
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF"   # optional
export TELEGRAM_CHAT_ID="987654321"          # optional
```

Pick a long, unguessable topic name — ntfy.sh topics are readable by anyone who
knows the name. Subscribe to the same topic in the ntfy app, then verify before
relying on it:

```bash
python stock_watch.py --test-alert
```

In-stock alerts go out at priority 5 with a `Click` header pointing at the
product page, and include the branch list, distinguishing stores you can walk
into from warehouse-only stock. Lower-priority alerts (3) cover price changes,
the registration state, repeated fetch failures, and lost per-SKU coverage.

While stock persists the alert repeats every 10 minutes so a single missed push
isn't fatal. State persists to disk, so a restart doesn't re-alert for a state
already acknowledged.

**On iOS:** priority 4/5 sets an interruption level that should bypass Focus and
Do Not Disturb, but the app does not have Apple Critical Alerts wired up — it
may not ring through the hardware silent switch. Enable Time Sensitive
notifications and add ntfy to your Focus allow-list; don't rely on it piercing
silent mode.

## Usage

| Command | Purpose |
|---|---|
| `--check` | One fetch, full diagnostics: verdict, matched phrases, every SKU, branches, raw buy-box text |
| `--test-alert` | Send a test through every configured channel |
| `--once` | Single check. Exit `0` in stock, `1` not, `2` error |
| *(no flag)* | Continuous loop, ~90s with jitter |

Other flags: `--url`, `--label`, `--interval`, `--repeat-alert-min`,
`--watch-category`, `--no-state`.

`--check` is the debugging entry point — run it first when anything looks wrong.

## Running it continuously

`.github/workflows/watch.yml` runs `--once` on a `*/5` cron plus manual
dispatch. Add `NTFY_TOPIC` (and optionally the Telegram pair) as **repository
secrets**; never commit them.

To verify the push path without waiting for a restock:

```bash
gh workflow run watch.yml -f test_alert=true
```

Two honest constraints:

- **Scheduled workflows are not punctual.** `*/5` is a floor, not a guarantee;
  GitHub deprioritises cron under load. Expect detection in **5–25 minutes**.
  Activation of a newly added schedule can itself take 10–20 minutes.
- **Billing.** `*/5` is 288 runs/day, and jobs bill a one-minute minimum —
  roughly 8,640 minutes/month against a 2,000-minute free private-repo
  allowance, exhausted in about a week. **Actions minutes are free on public
  repos**; otherwise raise the interval to `*/30` (~1,440 min/month).

State is carried between ephemeral runners via `actions/cache` with a unique
key per run and prefix restore. A cache miss costs at most one duplicate
alert — never a missed one.

## Politeness

`robots.txt` permits product pages. The watcher sends a real User-Agent and
`Accept-Language: lt-LT`, polls with jitter, and backs off exponentially on
errors, alerting after five consecutive failures. It does not evade rate
limiting or work around blocking — if the site starts refusing, it backs off
and tells you. No cart automation or checkout scripting.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

43 tests run entirely against trimmed HTML fixtures — no live traffic. They
cover diacritics folding, all three states, per-SKU classification, branch
parsing, and the failure modes above.

## Requirements

Python 3.9+, `requests`, `beautifulsoup4`. On Windows use the `py` launcher.
No browser or JavaScript runtime — the site is server-rendered.
