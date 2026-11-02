"""Exercise the local dashboard over HTTP without LLM or market-data calls."""

import http.client
import json
import socket
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli.dashboard import Dashboard, DashboardServer, run_report
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import write_report_tree


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    connect = socket.socket.connect

    def local_only(sock, address):
        if address[0] != "127.0.0.1":
            raise OSError("Only loopback traffic is allowed in dashboard tests")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


@pytest.fixture(autouse=True)
def yahoo_search(monkeypatch):
    quotes = [
        {"symbol": ticker, "longname": name, "exchDisp": exchange, "quoteType": "EQUITY"}
        for ticker, name, exchange in (
            ("AAPL", "Apple Inc.", "NASDAQ"),
            ("MSFT", "Microsoft Corporation", "NASDAQ"),
            ("0700.HK", "Tencent Holdings Limited", "Hong Kong"),
        )
    ]
    calls = []

    def search(query, **kwargs):
        calls.append(query)
        matches = [q for q in quotes if query.lower() in (q["symbol"] + " " + q["longname"]).lower()]
        return SimpleNamespace(response={"quotes": matches})

    monkeypatch.setattr("tradingagents.dataflows.vendors.yahoo.search.yf.Search", search)
    return calls


@pytest.fixture(autouse=True)
def yahoo_profile(monkeypatch):
    calls = []

    def profile(ticker):
        calls.append(ticker)
        return {
            "longName": "Apple Inc.",
            "regularMarketPrice": 123.45, "currency": "USD",
            "regularMarketTime": 1767376800, "sector": "Technology",
        }

    monkeypatch.setattr("cli.quotes.get_company_profile", profile)
    return calls


@pytest.fixture
def config(tmp_path):
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["results_dir"] = str(tmp_path / "results")
    cfg["backend_url"] = "https://secret-endpoint.invalid/private-key"
    return cfg


def saved_report(ticker, date, config):
    path = Path(config["results_dir"]) / "reports" / ticker
    state = {
        "trade_date": date,
        "market_report": "Verified test report. <script>alert('bad')</script>",
        "final_trade_decision": "HOLD",
    }
    report = write_report_tree(state, ticker, path)
    return report.with_suffix(".html"), "Hold"


@pytest.fixture
def server(config):
    with DashboardServer(config, port=0, runner=saved_report) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server
        if server.dashboard.worker:
            server.dashboard.worker.join(timeout=5)
            assert not server.dashboard.worker.is_alive()
        server.shutdown()
        thread.join(timeout=5)
        if server.dashboard.quotes.worker:
            server.dashboard.quotes.worker.join(timeout=5)
            assert not server.dashboard.quotes.worker.is_alive()


def request(server, path="/api/stocks", data=None, headers=None, raw=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    body = raw if raw is not None else (json.dumps(data) if data is not None else None)
    method = "POST" if body is not None else "GET"
    request_headers = {
        "Origin": f"http://127.0.0.1:{server.server_port}",
        "Content-Type": "application/json",
    }
    request_headers.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read().decode()
        if "application/json" in response.getheader("Content-Type", ""):
            content = json.loads(content)
        return response.status, content, dict(response.getheaders())
    finally:
        connection.close()


def wait_for_report(server):
    server.dashboard.worker.join(timeout=5)
    assert not server.dashboard.worker.is_alive()
    return request(server)[1]["stocks"][0]


def test_watchlist_survives_restart_and_deduplicates_symbols(server, config):
    assert request(server)[1]["stocks"] == []
    for ticker in (" aapl ", "AAPL", "700.HK"):
        assert request(server, "/api/add", {"ticker": ticker})[0] == 200
    assert [s["ticker"] for s in request(server)[1]["stocks"]] == ["0700.HK", "AAPL"]
    reopened = Dashboard(config)
    assert [s["ticker"] for s in reopened.snapshot()["stocks"]] == ["0700.HK", "AAPL"]
    assert request(server, "/api/remove", {"ticker": "AAPL"})[0] == 200
    assert [s["ticker"] for s in reopened.snapshot()["stocks"]] == ["0700.HK"]


def test_quotes_for_existing_stocks_are_cached_and_do_not_start_analysis(server, yahoo_profile):
    request(server, "/api/add", {"ticker": "AAPL"})
    request(server)
    server.dashboard.quotes.worker.join(timeout=5)
    quote = request(server)[1]["stocks"][0]["quote"]
    assert quote == {
        "name": "Apple Inc.", "price": 123.45, "currency": "USD", "sector": "Technology",
        "quoted_at": "2026-01-02T18:00:00+00:00", "refreshing": False, "error": None,
    }
    for _ in range(3):
        assert request(server)[1]["stocks"][0]["quote"] == quote
    assert yahoo_profile == ["AAPL"]
    assert server.dashboard.worker is None


def test_slow_quote_does_not_block_watchlist_or_reports(server, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_profile(ticker):
        started.set()
        assert release.wait(timeout=5)
        raise TimeoutError("private-provider-url")

    monkeypatch.setattr("cli.quotes.get_company_profile", slow_profile)
    request(server, "/api/add", {"ticker": "AAPL"})
    try:
        assert request(server)[1]["stocks"][0]["quote"]["refreshing"]
        assert started.wait(timeout=2)
        assert request(server, "/")[0] == 200
        assert request(server, "/api/analyze", {"ticker": "AAPL"})[0] == 200
        assert wait_for_report(server)["status"] == "done"
    finally:
        release.set()
    server.dashboard.quotes.worker.join(timeout=5)
    quote = request(server)[1]["stocks"][0]["quote"]
    assert quote["error"] and "private-provider-url" not in quote["error"]
    assert not quote["refreshing"]


def test_search_then_add_verifies_the_selected_symbol_again(server, yahoo_search):
    status, data, _ = request(server, "/api/search?q=Apple")
    assert status == 200
    assert data["results"] == [{"ticker": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ"}]
    assert request(server)[1]["stocks"] == []
    assert request(server, "/api/add", {"ticker": "AAPL"})[0] == 200
    assert yahoo_search == ["Apple", "AAPL"]
    assert request(server)[1]["stocks"][0]["status"] == "idle"
    assert server.dashboard.worker is None


@pytest.mark.parametrize("ticker", ["DOESNOTEXIST", "APPLE"])
def test_unknown_or_nonexact_symbol_cannot_be_added_directly(server, ticker):
    assert request(server, "/api/add", {"ticker": ticker})[0] == 422
    assert request(server)[1]["stocks"] == []


def test_search_without_matches_is_not_an_outage(server):
    status, data, _ = request(server, "/api/search?q=DOESNOTEXIST")
    assert status == 200 and data == {"results": []}


@pytest.mark.parametrize("query", ["", "%20", "x" * 81])
def test_search_validates_queries_before_contacting_yahoo(server, yahoo_search, query):
    assert request(server, "/api/search?q=" + query)[0] == 400
    assert yahoo_search == []


@pytest.mark.parametrize("response", [None, {}, {"quotes": None}, {"quotes": [None]}, {"quotes": [], "error": "private"}])
def test_broken_provider_responses_are_not_reported_as_no_matches(server, monkeypatch, response):
    monkeypatch.setattr("tradingagents.dataflows.vendors.yahoo.search.yf.Search", lambda *a, **kw: SimpleNamespace(response=response))
    for path, body in (("/api/search?q=Apple", None), ("/api/add", {"ticker": "AAPL"})):
        status, data, _ = request(server, path, body)
        assert status == 503 and "private" not in data["error"]
    assert request(server)[1]["stocks"] == []


def test_provider_failure_does_not_block_existing_watchlist_operations(server, monkeypatch):
    request(server, "/api/add", {"ticker": "AAPL"})

    def unavailable(*args, **kwargs):
        raise TimeoutError("secret-provider-url")

    monkeypatch.setattr("tradingagents.dataflows.vendors.yahoo.search.yf.Search", unavailable)
    for path, body in (("/api/search?q=Apple", None), ("/api/add", {"ticker": "MSFT"})):
        status, data, _ = request(server, path, body)
        assert status == 503 and "secret-provider-url" not in data["error"]
    assert [s["ticker"] for s in request(server)[1]["stocks"]] == ["AAPL"]
    assert request(server, "/api/remove", {"ticker": "AAPL"})[0] == 200


def test_search_filters_other_instruments_duplicates_and_unsafe_symbols(server, monkeypatch):
    quote = {"symbol": "AAPL", "shortname": "Apple", "exchange": "NMS", "quoteType": "EQUITY"}
    quotes = [quote, quote, {**quote, "symbol": "../bad"}, {**quote, "symbol": "SPY", "quoteType": "ETF"}]
    monkeypatch.setattr("tradingagents.dataflows.vendors.yahoo.search.yf.Search", lambda *a, **kw: SimpleNamespace(response={"quotes": quotes}))
    assert request(server, "/api/search?q=Apple")[1] == {
        "results": [{"ticker": "AAPL", "name": "Apple", "exchange": "NMS"}],
    }
    assert request(server, "/api/add", {"ticker": "SPY"})[0] == 422


def test_analysis_saves_a_readable_report_and_preserves_it_after_failure(server, config):
    request(server, "/api/add", {"ticker": "AAPL"})
    assert request(server, "/api/analyze", {"ticker": "AAPL"})[0] == 200
    stock = wait_for_report(server)
    assert stock["status"] == "done"
    assert stock["rating"] == "Hold"
    assert stock["report_date"] == stock["analysis_date"]
    assert "report_file" not in stock
    status, html, headers = request(server, stock["report_url"])
    assert status == 200 and "Verified test report" in html
    assert "<script>alert" not in html
    assert "sandbox" in headers["Content-Security-Policy"]
    assert Dashboard(config).snapshot()["stocks"][0]["report_url"] == stock["report_url"]

    def fail(*args):
        raise RuntimeError("secret-provider-token must never appear")

    server.dashboard.runner = fail
    request(server, "/api/analyze", {"ticker": "AAPL"})
    failed = wait_for_report(server)
    assert failed["status"] == "error"
    assert "secret-provider-token" not in failed["error"]
    assert failed["report_date"] == stock["report_date"]
    assert request(server, failed["report_url"])[1] == html
    report_path = next(Path(config["results_dir"]).rglob("complete_report.html"))
    request(server, "/api/remove", {"ticker": "AAPL"})
    assert report_path.is_file()
    assert request(server, stock["report_url"])[0] == 404


def test_only_one_analysis_runs_and_the_page_remains_responsive(server):
    started, release = threading.Event(), threading.Event()

    def slow_report(*args):
        started.set()
        assert release.wait(timeout=5)
        return saved_report(*args)

    server.dashboard.runner = slow_report
    request(server, "/api/add", {"ticker": "AAPL"})
    try:
        assert request(server, "/api/analyze", {"ticker": "AAPL"})[0] == 200
        assert started.wait(timeout=2)
        assert request(server)[1]["busy"] is True
        assert request(server, "/")[0] == 200
        assert request(server, "/api/add", {"ticker": "MSFT"})[0] == 200
        assert request(server, "/api/analyze", {"ticker": "MSFT"})[0] == 409
        assert request(server, "/api/analyze", {"ticker": "AAPL"})[0] == 409
        assert request(server, "/api/remove", {"ticker": "AAPL"})[0] == 409
    finally:
        release.set()
    assert wait_for_report(server)["status"] == "done"
    assert request(server)[1]["busy"] is False


def test_restart_marks_interrupted_work_without_retrying(config):
    dashboard = Dashboard(config)
    dashboard.change("add", "AAPL")
    with sqlite3.connect(dashboard.database) as db:
        db.execute("UPDATE watchlist SET status = 'running'")
    reopened = Dashboard(config)
    stock = reopened.snapshot()["stocks"][0]
    assert stock["status"] == "error"
    assert "interrumpió" in stock["error"]
    assert reopened.worker is None


@pytest.mark.parametrize("ticker", ["../secret", "..", "<script>", "", "A" * 33, None, 42, ["AAPL"]])
def test_invalid_symbols_are_not_saved(server, ticker):
    assert request(server, "/api/add", {"ticker": ticker})[0] == 400
    assert request(server)[1]["stocks"] == []


def test_http_boundaries_do_not_expose_files_or_allow_cross_site_writes(server):
    assert request(server, "/api/add", {"ticker": "AAPL"}, headers={"Origin": "https://attacker.invalid"})[0] == 403
    assert request(server, headers={"Host": "attacker.invalid"})[0] == 403
    assert request(server, "/api/add", {"ticker": "AAPL"}, headers={"Origin": ""})[0] == 403
    assert request(server, "/api/add", {"ticker": "AAPL"}, headers={"Content-Type": "text/plain"})[0] == 400
    assert request(server, "/api/add", raw="{")[0] == 400
    assert request(server, "/api/add", raw="[]")[0] == 400
    assert request(server, "/api/add", raw="x" * 1025)[0] == 400
    assert request(server, "/api/unknown", {"ticker": "AAPL"})[0] == 404
    assert request(server, "/api/analyze", {"ticker": "AAPL"})[0] == 400
    for path in ("/.env", "/../.env", "/report?ticker=../../.env", "/report?ticker=AAPL"):
        assert request(server, path)[0] == 404
    assert "secret-endpoint" not in json.dumps(request(server)[1])
    assert request(server)[1]["stocks"] == []


def test_missing_and_outside_reports_are_not_served(server, tmp_path):
    request(server, "/api/add", {"ticker": "AAPL"})
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    with sqlite3.connect(server.dashboard.database) as db:
        db.execute("UPDATE watchlist SET report_file = ?", (str(outside),))
    assert request(server, "/report?ticker=AAPL")[0] == 404
    with sqlite3.connect(server.dashboard.database) as db:
        db.execute("UPDATE watchlist SET report_file = ?", (str(server.dashboard.database.parent / "missing.html"),))
    assert request(server, "/report?ticker=AAPL")[0] == 404


def test_runner_uses_existing_graph_and_report_writer(config, monkeypatch):
    import tradingagents.graph.trading_graph as graph_module

    seen = {}

    class Graph:
        def __init__(self, selected_analysts, config):
            seen["analysts"] = selected_analysts
            seen["config"] = config

        def propagate(self, ticker, date):
            seen["inputs"] = (ticker, date)
            return {"market_report": "Real report writer"}, "Hold"

        def save_reports(self, state, ticker, save_path, html):
            return write_report_tree(state, ticker, save_path, html=html)

    monkeypatch.setattr(graph_module, "TradingAgentsGraph", Graph)
    report, rating = run_report("AAPL", "2026-01-02", config)
    assert report.is_file() and "Real report writer" in report.read_text()
    assert rating == "Hold"
    assert seen["analysts"] == ["market", "news", "fundamentals"]
    assert seen["inputs"] == ("AAPL", "2026-01-02")
    assert seen["config"] == config


def test_dashboard_command_does_not_start_an_interactive_analysis(monkeypatch):
    import cli.dashboard as dashboard_module
    import cli.main as main

    calls = []
    monkeypatch.setattr(dashboard_module, "serve_dashboard", lambda config: calls.append("dashboard"))
    monkeypatch.setattr(main, "run_analysis", lambda **kw: calls.append("analysis"))
    result = CliRunner().invoke(main.app, ["dashboard"])
    assert result.exit_code == 0, result.output
    assert calls == ["dashboard"]
