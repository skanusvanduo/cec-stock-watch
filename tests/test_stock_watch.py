"""Offline tests for the cec.lt watcher.

Every case runs against saved fixtures, so the suite never touches the live
site. Run with:  py -m pytest -q
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from stock_watch import (  # noqa: E402
    Snapshot,
    build_alert,
    category_tiles,
    fold,
    parse_price,
    variant_label,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def out_of_stock():
    return Snapshot(load("out_of_stock.html"))


@pytest.fixture(scope="module")
def in_stock():
    return Snapshot(load("in_stock.html"))


# --------------------------------------------------------------------------
# Diacritics folding - the real strings from the live pages
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Į krepšelį", "i krepseli"),
    ("Prekės šiuo metu neturime", "prekes siuo metu neturime"),
    ("Registracija įsigijimui", "registracija isigijimui"),
    ("Prekę turime:", "preke turime:"),
    ("Informuoti", "informuoti"),
    ("Klaipėda", "klaipeda"),
    ("Šiauliai", "siauliai"),
    ("Centrinis sandėlis", "centrinis sandelis"),
    # Every Lithuanian diacritic, upper case.
    ("ĄČĘĖĮŠŲŪŽ", "aceeisuuz"),
    # NFKD turns the non-breaking thousands separator into a plain space.
    ("Kaina 1 459,00 €", "kaina 1 459,00 €"),
])
def test_fold(raw, expected):
    assert fold(raw) == expected


def test_fold_collapses_whitespace():
    assert fold("  Į   krepšelį \n ") == "i krepseli"


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_out_of_stock_page_classifies(out_of_stock):
    assert out_of_stock.status == "out_of_stock"
    assert out_of_stock.conflict is False
    assert out_of_stock.matched["buyable"] == []
    assert "prekes siuo metu neturime" in out_of_stock.matched["out_of_stock"]


def test_in_stock_page_classifies(in_stock):
    assert in_stock.status == "in_stock"
    assert in_stock.conflict is False
    assert in_stock.matched["buyable"] == ["i krepseli"]


def test_buybox_is_scoped_in_both_states(out_of_stock, in_stock):
    """If this fails the site was rebuilt and the class prefix moved."""
    assert out_of_stock.scoped
    assert in_stock.scoped


def test_price_extracted(out_of_stock, in_stock):
    assert out_of_stock.price == "1 459,00 €"
    assert in_stock.price == "55,00 €"


def test_parse_price_handles_nbsp():
    assert parse_price("Kaina 1 459,00 €") == "1 459,00 €"
    assert parse_price("no price here") is None


# --------------------------------------------------------------------------
# Per-SKU variants (the colour question)
# --------------------------------------------------------------------------

def test_target_page_exposes_all_colour_variants(out_of_stock):
    """One fetch must cover every colour, not just the rendered default."""
    assert len(out_of_stock.variants) == 9
    colours = {v["color"] for v in out_of_stock.variants}
    assert colours == {
        "Dangaus mėlyna",      # Sky Blue
        "Sidabrinė",           # Silver
        "Vidurnakčio juoda",   # Midnight
        "Žvaigždžių šviesos",  # Starlight
    }
    assert all(v["availability"] == "OutOfStock" for v in out_of_stock.variants)
    assert out_of_stock.in_stock_variants == []


def test_missing_jsonld_availability_is_not_out_of_stock(in_stock):
    """The in-stock fixture carries no availability field at all.

    Absence must never be read as OutOfStock, or every accessory page would
    look out of stock forever.
    """
    assert in_stock.variants == []
    assert in_stock.status == "in_stock"


# --------------------------------------------------------------------------
# Branch list
# --------------------------------------------------------------------------

def test_branches_parsed_with_cities(in_stock):
    cities = {c for c, _ in in_stock.stores if c}
    assert {"Vilnius", "Kaunas", "Klaipėda", "Šiauliai"} <= cities
    vilnius = next(s for c, s in in_stock.stores if c == "Vilnius")
    assert "PLC „Panorama“" in vilnius
    assert len(vilnius) == 3


def test_warehouse_has_no_city(in_stock):
    warehouse = [s for c, s in in_stock.stores if not c]
    assert warehouse == [["Centrinis sandėlis"]]


def test_vilnius_detected(in_stock):
    assert in_stock.has_vilnius() is True


def test_out_of_stock_has_no_branches(out_of_stock):
    assert out_of_stock.stores == []
    assert out_of_stock.store_summary() == ""


def test_store_summary_puts_walk_in_first(in_stock):
    summary = in_stock.store_summary()
    assert summary.startswith("Walk in and buy")
    assert "Vilnius" in summary
    assert "Centrinis sandėlis" in summary


def test_warehouse_only_is_labelled():
    html = """
    <div class="Product_add_to_cart_wrapper__x">Kaina 999,00 &euro;
      <div>&#302; krep&#353;el&#303;</div>
      <div class="Stocks_stores_wrapper__x">
        <div class="Stocks_stores_city__x">
          <div class="Stocks_store__x">Centrinis sand&#279;lis</div>
        </div>
      </div>
    </div>"""
    snap = Snapshot(html)
    assert snap.status == "in_stock"
    assert snap.has_vilnius() is False
    assert snap.store_summary().startswith("Warehouse only")


# --------------------------------------------------------------------------
# Failure modes from brief section 8
# --------------------------------------------------------------------------

def test_conflicting_phrases_alert_but_flag():
    """Both states present: bias toward alerting, but mark it."""
    html = """
    <div class="Product_add_to_cart_wrapper__x">
      Kaina 1&nbsp;459,00 &euro;
      <div>&#302; krep&#353;el&#303;</div>
      <div>Prek&#279;s &#353;iuo metu neturime</div>
    </div>"""
    snap = Snapshot(html)
    assert snap.status == "in_stock"
    assert snap.conflict is True
    _, body = build_alert(snap)
    assert "both in-stock and out-of-stock" in body


def test_missing_buybox_is_unknown_not_out_of_stock():
    snap = Snapshot("<html><body><p>Nothing useful here</p></body></html>")
    assert snap.scoped is False
    assert snap.status == "unknown"


def test_related_products_carousel_does_not_leak_in():
    """An in-stock item below the buy box must not flip our verdict."""
    html = """
    <div class="Product_add_to_cart_wrapper__x">
      Kaina 1&nbsp;459,00 &euro;
      <div>Prek&#279;s &#353;iuo metu neturime, spauskite informuoti.</div>
    </div>
    <div class="related">Susij&#281; produktai
      <div>&#302; krep&#353;el&#303;</div>
    </div>"""
    snap = Snapshot(html)
    assert snap.status == "out_of_stock"
    assert snap.matched["buyable"] == []


def test_preorder_state_detected():
    html = """
    <div class="Product_add_to_cart_wrapper__x">
      Kaina 1&nbsp;459,00 &euro;
      <div>Registracija &#303;sigijimui</div>
    </div>"""
    assert Snapshot(html).status == "preorder_registration"


def test_scripts_are_stripped_before_matching():
    """A JSON blob mentioning the cart phrase must not create a match."""
    html = """
    <div class="Product_add_to_cart_wrapper__x">
      Kaina 1&nbsp;459,00 &euro;
      <script>var label = "\\u012e krep\\u0161el\\u012f";</script>
      <div>Prek&#279;s &#353;iuo metu neturime</div>
    </div>"""
    snap = Snapshot(html)
    assert snap.status == "out_of_stock"


# --------------------------------------------------------------------------
# Alert composition
# --------------------------------------------------------------------------

def test_alert_mentions_vilnius_in_title(in_stock):
    title, body = build_alert(in_stock)
    assert "VILNIUS" in title
    assert "Vilnius" in body
    assert "55,00" in body


def test_alert_without_branches_still_builds():
    html = ('<div class="Product_add_to_cart_wrapper__x">Kaina 10,00 &euro;'
            '<div>&#302; krep&#353;el&#303;</div></div>')
    title, body = build_alert(Snapshot(html))
    assert "IN STOCK" in title
    assert "VILNIUS" not in title
    assert body


# --------------------------------------------------------------------------
# Category page (new-SKU risk)
# --------------------------------------------------------------------------

def test_category_tiles_matches_16gb_512gb():
    html = """
    <a href="/mac/macbook-air-m5/macbook-air-13-m5-10-core-cpu-8-core-gpu-16gb-512gb">a</a>
    <a href="/mac/macbook-air-m5/macbook-air-13-m5-10-core-cpu-10-core-gpu-24gb-1tb">b</a>
    <a href="/mac/macbook-air-m5/macbook-air-13-m5-16gb-512gb-midnight-int?x=1">c</a>
    <a href="/unrelated/thing">d</a>"""
    all_paths, matching = category_tiles(html)
    assert len(all_paths) == 3
    assert len(matching) == 2
    assert all("16gb" in p and "512gb" in p for p in matching)


# --------------------------------------------------------------------------
# Any colour, any keyboard layout counts (user requirement)
# --------------------------------------------------------------------------

def _flip_sku_to_instock(html, sku):
    """Flip one SKU's availability in the saved JSON-LD."""
    needle = '"sku":"%s"' % sku
    assert needle in html, f"{sku} not in fixture"
    head, tail = html.split(needle, 1)
    tail = tail.replace("https://schema.org/OutOfStock",
                        "https://schema.org/InStock", 1)
    return head + needle + tail


def test_all_nine_colour_layout_combos_are_visible(out_of_stock):
    combos = sorted({variant_label(v) for v in out_of_stock.variants})
    # 9 SKUs collapse to 7 combos: Silver/INT and Midnight/INT each have two
    # SKUs (a stock code and a built-to-order code). Sky Blue has no US layout.
    assert len(combos) == 7
    assert "Sky Blue / US" not in combos
    layouts = {c.split(" / ")[1] for c in combos}
    assert layouts == {"INT", "US"}
    colours = {c.split(" / ")[0] for c in combos}
    assert colours == {"Sky Blue", "Silver", "Midnight", "Starlight"}


@pytest.mark.parametrize("sku,expected", [
    ("MDHE4ZE/A", "Midnight / INT"),
    ("Z1L60005Y", "Midnight / US"),
    ("Z1L30005Y", "Starlight / US"),
    ("MDH74ZE/A", "Silver / INT"),
    ("MDHH4ZE/A", "Sky Blue / INT"),
])
def test_any_single_variant_in_stock_triggers_alert(sku, expected):
    """The buy box still reads out-of-stock; JSON-LD must win anyway.

    This is the case that would otherwise be missed entirely: a colour or
    keyboard layout other than the page default comes back in stock.
    """
    html = _flip_sku_to_instock(load("out_of_stock.html"), sku)
    snap = Snapshot(html)

    # The rendered buy box has NOT changed - still the notify-me state.
    assert snap.matched["buyable"] == []
    assert "prekes siuo metu neturime" in snap.matched["out_of_stock"]

    # ...but we alert regardless, and name the combination to select.
    assert snap.status == "in_stock"
    assert [v["sku"] for v in snap.in_stock_variants] == [sku]

    title, body = build_alert(snap)
    assert "IN STOCK" in title
    assert expected in body
    assert "colour and keyboard selectors" in body


def test_multiple_variants_in_stock_are_all_listed():
    html = load("out_of_stock.html")
    html = _flip_sku_to_instock(html, "MDHE4ZE/A")   # Midnight / INT
    html = _flip_sku_to_instock(html, "Z1L30005Y")   # Starlight / US
    snap = Snapshot(html)
    assert snap.status == "in_stock"
    _, body = build_alert(snap)
    assert "Midnight / INT" in body
    assert "Starlight / US" in body
    assert "Available now (2)" in body


def test_variant_label_falls_back_without_a_name():
    assert variant_label({"sku": "X1", "color": "Sidabrinė"}) == "Sidabrinė"
    assert variant_label({"sku": "X1"}) == "X1"
    assert variant_label({}) == "unknown variant"


# --------------------------------------------------------------------------
# Per-SKU coverage guard
# --------------------------------------------------------------------------

class _Args:
    url = "https://example.test/product"
    label = "Test product"
    repeat_alert_min = 10
    no_state = True
    watch_category = False


def _run_once_with(monkeypatch, html, state):
    """Drive run_once against fixed HTML, capturing notifications."""
    import stock_watch as sw
    sent = []
    monkeypatch.setattr(sw, "fetch", lambda url, timeout=30: html)
    monkeypatch.setattr(sw, "notify",
                        lambda title, body, url, urgent=True, tags="computer":
                        sent.append((title, body, urgent)))
    snap = sw.run_once(_Args(), state)
    return snap, sent


def test_losing_jsonld_coverage_raises_an_alert(monkeypatch):
    """JSON-LD disappearing must not fail silently.

    Text still reads out-of-stock, so nothing else would complain - but we
    have quietly stopped watching six of the seven combinations.
    """
    rich = load("out_of_stock.html")
    stripped = '<div class="Product_add_to_cart_wrapper__x">Kaina 1&nbsp;459,00 &euro;' \
               '<div>Prek&#279;s &#353;iuo metu neturime</div></div>'
    state = {}

    snap1, sent1 = _run_once_with(monkeypatch, rich, state)
    assert len(snap1.variants) == 9
    assert sent1 == []

    snap2, sent2 = _run_once_with(monkeypatch, stripped, state)
    assert snap2.variants == []
    assert snap2.status == "out_of_stock"      # looks healthy...
    assert len(sent2) == 1                     # ...but we are told
    title, body, urgent = sent2[0]
    assert "coverage" in title.lower()
    assert "9 SKUs" in body
    assert urgent is False                     # priority 3, not 5


def test_no_coverage_alert_when_page_never_had_variants(monkeypatch):
    """Accessory pages carry no availability at all - that is not a fault."""
    plain = '<div class="Product_add_to_cart_wrapper__x">Kaina 39,00 &euro;' \
            '<div>&#302; krep&#353;el&#303;</div></div>'
    state = {}
    _run_once_with(monkeypatch, plain, state)
    _, sent = _run_once_with(monkeypatch, plain, state)
    assert [t for t, _, _ in sent if "coverage" in t.lower()] == []


def test_coverage_restored_does_not_alert(monkeypatch):
    rich = load("out_of_stock.html")
    state = {}
    _run_once_with(monkeypatch, rich, state)
    _, sent = _run_once_with(monkeypatch, rich, state)
    assert [t for t, _, _ in sent if "coverage" in t.lower()] == []
