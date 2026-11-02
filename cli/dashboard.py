"""Local watchlist and on-demand research, using the existing graph and reports."""

import json
import os
import sqlite3
import threading
from contextlib import closing, suppress
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from cli.quotes import MarketQuotes
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.search import search_stocks


class BusyError(Exception):
    pass


class UnknownStockError(Exception):
    pass


def run_report(ticker, trade_date, config):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(
        selected_analysts=["market", "news", "fundamentals"], config=config
    )
    state, rating = graph.propagate(ticker, trade_date)
    directory = Path(config["results_dir"]) / "reports" / f"{ticker}_{uuid4().hex}"
    report = graph.save_reports(state, ticker, save_path=directory, html=True)
    return report.with_suffix(".html"), rating


class Dashboard:
    def __init__(self, config, runner=run_report):
        self.config = deepcopy(config)
        if not os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE"):
            self.config["output_language"] = "Spanish"
        self.runner = runner
        self.lock = threading.Lock()
        self.worker = None
        self.quotes = MarketQuotes()
        self.database = Path(config["results_dir"]) / "watchlist.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS watchlist (
                ticker TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'idle',
                analysis_date TEXT, report_date TEXT, rating TEXT,
                report_file TEXT, error TEXT
            )""")
            db.execute("""UPDATE watchlist SET status = 'error', error = ?
                WHERE status = 'running'""", (
                "El análisis se interrumpió. Puedes volver a generarlo.",
            ))

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    def snapshot(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM watchlist ORDER BY ticker").fetchall()
        quotes = self.quotes.snapshot([row["ticker"] for row in rows])
        stocks = []
        for row in rows:
            stock = dict(row)
            stock["quote"] = quotes[stock["ticker"]]
            report = stock.pop("report_file")
            stock["report_url"] = (
                "/report?ticker=" + quote(stock["ticker"], safe="") if report else None
            )
            stocks.append(stock)
        cfg = self.config
        return {
            "stocks": stocks,
            "busy": any(stock["status"] == "running" for stock in stocks),
            "models": {
                tier: {
                    "provider": cfg.get(f"{tier}_think_provider") or cfg["llm_provider"],
                    "model": cfg[f"{tier}_think_llm"],
                } for tier in ("quick", "deep")
            },
        }

    def change(self, action, raw_ticker):
        if not isinstance(raw_ticker, str):
            raise ValueError("Introduce un símbolo, por ejemplo AAPL o 0700.HK.")
        ticker = safe_ticker_component(normalize_symbol(raw_ticker))
        if action == "add" and not any(stock["ticker"] == ticker for stock in search_stocks(ticker)):
            raise UnknownStockError("No se pudo confirmar esa acción. Búscala de nuevo y selecciona un resultado.")
        with self.lock, closing(self.connect()) as db, db:
            row = db.execute("SELECT * FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
            if action == "add":
                db.execute("INSERT OR IGNORE INTO watchlist (ticker) VALUES (?)", (ticker,))
                return
            if row is None:
                raise ValueError("La acción no está en tu lista.")
            if action == "remove":
                if row["status"] == "running":
                    raise BusyError("Espera a que termine el análisis antes de quitar la acción.")
                db.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
                return
            if action != "analyze":
                raise ValueError("Acción no válida.")
            if db.execute("SELECT 1 FROM watchlist WHERE status = 'running'").fetchone():
                raise BusyError("Ya hay un análisis en curso. Espera a que termine.")
            trade_date = get_current_date()
            db.execute("""UPDATE watchlist SET status = 'running', analysis_date = ?,
                error = NULL WHERE ticker = ?""", (trade_date, ticker))
            db.commit()
            self.worker = threading.Thread(target=self._analyze, args=(ticker, trade_date), daemon=True)
            try:
                self.worker.start()
            except RuntimeError:
                db.execute("UPDATE watchlist SET status = 'error', error = ? WHERE ticker = ?", (
                    "No se pudo iniciar el análisis. Vuelve a intentarlo.", ticker,
                ))
                db.commit()
                raise

    def _analyze(self, ticker, trade_date):
        try:
            report, rating = self.runner(ticker, trade_date, deepcopy(self.config))
            report = Path(report).resolve()
            if not report.is_relative_to(Path(self.config["results_dir"]).resolve()):
                raise ValueError("Report outside results directory")
            if not report.is_file():
                raise FileNotFoundError("Report was not saved")
            with closing(self.connect()) as db, db:
                db.execute("""UPDATE watchlist SET status = 'done', report_file = ?,
                    report_date = ?, rating = ?, error = NULL WHERE ticker = ?""", (
                    str(report), trade_date, str(rating), ticker,
                ))
        except Exception as exc:
            message = (
                f"No se pudo completar el informe ({type(exc).__name__}). "
                "Comprueba en .env el proveedor, los modelos y sus credenciales; "
                "revisa también la conexión y los límites de uso antes de reintentar."
            )
            with closing(self.connect()) as db, db:
                db.execute("UPDATE watchlist SET status = 'error', error = ? WHERE ticker = ?", (
                    message, ticker,
                ))

    def report(self, ticker):
        with closing(self.connect()) as db:
            row = db.execute("SELECT report_file FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
        if not row or not row["report_file"]:
            raise FileNotFoundError
        path = Path(row["report_file"]).resolve()
        if not path.is_relative_to(Path(self.config["results_dir"]).resolve()):
            raise FileNotFoundError
        return path.read_bytes()


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, config, port=8765, runner=run_report):
        super().__init__(("127.0.0.1", port), DashboardHandler)
        try:
            self.dashboard = Dashboard(config, runner)
        except Exception:
            self.server_close()
            raise


class DashboardHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_args):
        pass

    def _allowed(self, mutation=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        if host not in hosts:
            self._json(403, {"error": "Acceso solo desde el panel local."})
            return False
        origin = self.headers.get("Origin")
        if (mutation or origin) and origin != f"http://{host}":
            self._json(403, {"error": "Origen no permitido."})
            return False
        return True

    def _send(self, status, body, content_type, report=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        policy = "default-src 'none'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        policy += "; sandbox" if report else "; script-src 'self'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._allowed():
            return
        url = urlsplit(self.path)
        assets = {
            "/": ("dashboard.html", "text/html; charset=utf-8"),
            "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
            "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
        }
        try:
            if url.path in assets:
                name, content_type = assets[url.path]
                self._send(200, files("cli").joinpath("static", name).read_bytes(), content_type)
            elif url.path == "/api/stocks":
                self._json(200, self.server.dashboard.snapshot())
            elif url.path == "/api/search":
                query = parse_qs(url.query).get("q", [""])[0]
                self._json(200, {"results": search_stocks(query)})
            elif url.path == "/report":
                ticker = parse_qs(url.query).get("ticker", [""])[0]
                self._send(200, self.server.dashboard.report(ticker), "text/html; charset=utf-8", report=True)
            else:
                self._json(404, {"error": "No encontrado."})
        except VendorUnavailableError:
            self._json(503, {"error": "El buscador no está disponible. No se pudo verificar la acción; inténtalo de nuevo."})
        except ValueError:
            self._json(400, {"error": "Escribe un nombre o símbolo de entre 1 y 80 caracteres."})
        except FileNotFoundError:
            self._json(404, {"error": "El informe no está disponible. Genera uno nuevo."})
        except (OSError, sqlite3.Error):
            self._json(500, {"error": "No se pudieron leer los datos guardados."})

    def do_POST(self):
        if not self._allowed(mutation=True):
            return
        action = {"/api/add": "add", "/api/remove": "remove", "/api/analyze": "analyze"}.get(self.path)
        if action is None:
            self._json(404, {"error": "No encontrado."})
            return
        try:
            if self.headers.get("Content-Type") != "application/json":
                raise ValueError("Se requiere JSON.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError("Petición vacía o demasiado grande.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Petición no válida.")
            self.server.dashboard.change(action, data.get("ticker"))
            self._json(200, {"ok": True})
        except BusyError as exc:
            self._json(409, {"error": str(exc)})
        except UnknownStockError as exc:
            self._json(422, {"error": str(exc)})
        except VendorUnavailableError:
            self._json(503, {"error": "El buscador no está disponible. No se añadió la acción; inténtalo de nuevo."})
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "Petición no válida. Usa un símbolo de hasta 32 caracteres, como AAPL o 0700.HK."})
        except (OSError, sqlite3.Error, RuntimeError):
            self._json(500, {"error": "No se pudo guardar el cambio. Comprueba el espacio y los permisos del disco."})


def serve_dashboard(config):
    with DashboardServer(config) as server:
        print(f"TradingAgents: http://127.0.0.1:{server.server_port}", flush=True)
        print("Solo uso local. Ctrl+C para cerrar; un análisis en curso se interrumpirá.", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()
