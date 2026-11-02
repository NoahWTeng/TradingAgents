"""Equity search for watchlist discovery and exact-symbol verification."""

import yfinance as yf

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import safe_ticker_component

from .common import yf_retry


def search_stocks(query: str) -> list[dict[str, str]]:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
        raise ValueError("Search requires between 1 and 80 characters")
    result = yf_retry(
        lambda: yf.Search(
            query.strip(), max_results=8, news_count=0, lists_count=0,
            recommended=0, include_cb=False, timeout=10, raise_errors=True,
        ),
        max_retries=0,
    )
    response = result.response if result is not None else None
    if not isinstance(response, dict) or not isinstance(response.get("quotes"), list):
        raise VendorUnavailableError("Yahoo search did not return a quotes list")
    if response.get("error") or response.get("finance"):
        raise VendorUnavailableError("Yahoo search returned an error")
    stocks = []
    seen = set()
    for quote in response["quotes"]:
        if not isinstance(quote, dict):
            raise VendorUnavailableError("Yahoo search returned an invalid quote")
        if quote.get("quoteType") != "EQUITY":
            continue
        try:
            ticker = safe_ticker_component(quote.get("symbol"))
        except ValueError:
            continue
        name = quote.get("longname") or quote.get("shortname")
        exchange = quote.get("exchDisp") or quote.get("exchange")
        if not isinstance(name, str) or not isinstance(exchange, str) or ticker in seen:
            continue
        seen.add(ticker)
        stocks.append({"ticker": ticker, "name": name, "exchange": exchange})
    return stocks
