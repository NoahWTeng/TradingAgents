"""Non-blocking, short-lived market snapshots for the local watchlist."""

import math
import threading
import time
from datetime import UTC, datetime

from tradingagents.dataflows.vendors.yahoo.fundamentals import get_company_profile


class MarketQuotes:
    def __init__(self):
        self.lock = threading.Lock()
        self.worker = None
        self.values = {}
        self.next_refresh = {}

    def snapshot(self, tickers):
        with self.lock:
            self.values = {ticker: self.values.get(ticker, {}) for ticker in tickers}
            self.next_refresh = {ticker: self.next_refresh.get(ticker, 0) for ticker in tickers}
            due = [ticker for ticker in tickers if time.monotonic() >= self.next_refresh[ticker]]
            if due and (self.worker is None or not self.worker.is_alive()):
                for ticker in due:
                    self.values[ticker] = {**self.values[ticker], "refreshing": True}
                self.worker = threading.Thread(target=self._refresh, args=(due,), daemon=True)
                self.worker.start()
            return {ticker: dict(value) for ticker, value in self.values.items()}

    def _refresh(self, tickers):
        for ticker in tickers:
            update = {}
            error = None
            try:
                info = get_company_profile(ticker)
                name = info.get("longName") or info.get("shortName")
                if isinstance(name, str) and name.strip():
                    update["name"] = name.strip()
                sector = info.get("sector")
                if isinstance(sector, str) and sector.strip():
                    update["sector"] = sector.strip()
                price = info.get("regularMarketPrice")
                currency = info.get("currency")
                quoted_at = info.get("regularMarketTime")
                if (
                    isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price <= 0
                    or not isinstance(currency, str) or not currency.strip()
                    or isinstance(quoted_at, bool) or not isinstance(quoted_at, (int, float))
                    or not math.isfinite(quoted_at) or quoted_at <= 0
                ):
                    raise ValueError("Incomplete market quote")
                timestamp = datetime.fromtimestamp(quoted_at, UTC).isoformat()
                update.update(price=price, currency=currency.strip(), quoted_at=timestamp)
            except Exception:
                error = "No se pudo actualizar el precio. Se reintentará automáticamente."
            with self.lock:
                if ticker in self.values:
                    self.values[ticker] = {
                        **self.values[ticker], **update, "refreshing": False, "error": error,
                    }
                    self.next_refresh[ticker] = time.monotonic() + 60
