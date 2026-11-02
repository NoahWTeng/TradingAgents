"""Market cache behavior: no repeated fetches, honest units and safe failures."""

import pytest

from cli.quotes import MarketQuotes


@pytest.fixture
def market(monkeypatch):
    info = {
        "regularMarketPrice": 152.75, "currency": "GBp",
        "regularMarketTime": 1767376800, "sector": "Financial Services",
    }
    monkeypatch.setattr("cli.quotes.get_company_profile", lambda ticker: dict(info))
    return MarketQuotes(), info


def fetch(cache, ticker="TEST.L"):
    cache.snapshot([ticker])
    cache.worker.join(timeout=3)
    assert not cache.worker.is_alive()
    return cache.snapshot([ticker])[ticker]


def test_refresh_interval_and_recovery_preserve_price_units_and_timestamp(market, monkeypatch):
    cache, info = market
    clock = [1000]
    monkeypatch.setattr("cli.quotes.time.monotonic", lambda: clock[0])
    first = fetch(cache)
    assert first["currency"] == "GBp" and first["price"] == 152.75
    info["regularMarketPrice"] = 153.25
    clock[0] += 59
    assert fetch(cache) == first
    clock[0] += 1
    assert fetch(cache)["price"] == 153.25
    info.pop("regularMarketTime")
    clock[0] += 60
    stale = fetch(cache)
    assert stale["error"] and stale["price"] == 153.25
    assert stale["currency"] == first["currency"]
    assert stale["quoted_at"] == first["quoted_at"]
    info["regularMarketTime"] = 1767376860
    clock[0] += 60
    recovered = fetch(cache)
    assert recovered["error"] is None and recovered["quoted_at"] != first["quoted_at"]


@pytest.mark.parametrize("field,value", [
    ("regularMarketPrice", None), ("regularMarketPrice", float("nan")),
    ("regularMarketPrice", float("inf")), ("regularMarketPrice", True),
    ("currency", None), ("currency", ""),
    ("regularMarketTime", None), ("regularMarketTime", float("inf")),
    ("regularMarketTime", 1e30), ("regularMarketTime", True),
])
def test_incomplete_quotes_keep_sector_without_inventing_prices(market, field, value):
    cache, info = market
    info[field] = value
    result = fetch(cache)
    assert result["sector"] == "Financial Services"
    assert result["error"] and "price" not in result and "quoted_at" not in result


@pytest.mark.parametrize("long_name,short_name,expected", [
    ("  Example Company Holdings plc  ", "Example", "Example Company Holdings plc"),
    (None, "Example", "Example"),
    (None, None, None),
])
def test_company_name_and_fallback_survive_a_failed_price_refresh(market, long_name, short_name, expected):
    cache, info = market
    info.update(longName=long_name, shortName=short_name)
    result = fetch(cache)
    assert result.get("name") == expected
    info.clear()
    cache.next_refresh["TEST.L"] = 0
    failed = fetch(cache)
    assert failed["error"] and failed.get("name") == expected


def test_absent_sector_does_not_discard_valid_price(market):
    cache, info = market
    info.pop("sector")
    result = fetch(cache)
    assert result["price"] == 152.75 and result["error"] is None
    assert "sector" not in result


def test_removed_symbols_are_dropped_from_cache(market):
    cache, _ = market
    fetch(cache)
    assert cache.snapshot([]) == {}
    assert cache.next_refresh == {}
