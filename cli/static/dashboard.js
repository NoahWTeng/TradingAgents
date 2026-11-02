const form = document.querySelector("#search-form");
const input = document.querySelector("#query");
const searchStatus = document.querySelector("#search-status");
const searchResults = document.querySelector("#search-results");
let searchVersion = 0;
const error = document.querySelector("#error");
const stocks = document.querySelector("#stocks");
let lastSnapshot = "";
let sending = false;
const pageSize = 10;
let currentPage = 1;
let currentData = null;
const pagination = document.querySelector("#pagination");
const previousPage = document.querySelector("#previous-page");
const nextPage = document.querySelector("#next-page");

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

async function request(path, ticker) {
  const options = ticker === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ticker}),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "No se pudo completar la petición.");
  return data;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(label, action, ticker, disabled) {
  const button = element("button", label, action === "remove" ? "remove" : "");
  button.type = "button";
  button.disabled = disabled;
  button.dataset.action = action;
  button.dataset.ticker = ticker;
  button.setAttribute("aria-label", label + " " + ticker);
  return button;
}

function marketDetails(quote = {}) {
  const market = element("div", "", "market-details");
  const fields = element("dl", "", "market-fields");
  const price = element("div");
  const hasPrice = Number.isFinite(quote.price);
  const value = hasPrice
    ? new Intl.NumberFormat("es-ES", {minimumFractionDigits: 2, maximumFractionDigits: 10}).format(quote.price) + " " + quote.currency
    : quote.refreshing ? "Cargando…" : "No disponible";
  price.append(element("dt", "Último precio"), element("dd", value, "price"));
  const sector = element("div");
  sector.append(element("dt", "Sector"), element("dd", quote.sector || (quote.refreshing ? "Cargando…" : "No disponible")));
  fields.append(price, sector);
  market.append(fields);
  if (quote.quoted_at) {
    const date = new Date(quote.quoted_at);
    const timestamp = element("time", "Cotización: " + new Intl.DateTimeFormat("es-ES", {
      dateStyle: "short", timeStyle: "long",
    }).format(date), "quote-time");
    timestamp.dateTime = quote.quoted_at;
    market.append(timestamp);
  }
  if (quote.error) {
    market.append(element("p", (hasPrice ? "Dato desactualizado. " : "") + quote.error, "quote-warning"));
  } else if (quote.refreshing && hasPrice) {
    market.append(element("p", "Actualizando…", "quote-time"));
  }
  return market;
}

function render(data) {
  currentData = data;
  const totalPages = Math.max(1, Math.ceil(data.stocks.length / pageSize));
  currentPage = Math.min(currentPage, totalPages);
  const start = (currentPage - 1) * pageSize;
  pagination.hidden = data.stocks.length <= pageSize;
  previousPage.disabled = currentPage === 1;
  nextPage.disabled = currentPage === totalPages;
  document.querySelector("#page-status").textContent = data.stocks.length
    ? `${start + 1}–${Math.min(start + pageSize, data.stocks.length)} de ${data.stocks.length} · Página ${currentPage} de ${totalPages}`
    : "";
  document.querySelector("#count").textContent = data.stocks.length;
  document.querySelector("#empty").hidden = data.stocks.length !== 0;
  const active = data.stocks.find(stock => stock.status === "running");
  document.querySelector("#activity").textContent = active
    ? "Investigando " + active.ticker + " · Puedes dejar esta pestaña abierta"
    : "Solo se analiza cuando tú lo solicitas";
  document.querySelector("#models").textContent = ["quick", "deep"].map(tier =>
    (tier === "quick" ? "Analistas: " : "Responsables: ")
    + data.models[tier].provider + " / " + data.models[tier].model
  ).join(" · ");
  const focus = document.activeElement?.dataset;
  const fragment = document.createDocumentFragment();
  const statuses = {idle: "Sin analizar", running: "Analizando…", done: "Informe listo", error: "Requiere atención"};
  for (const stock of data.stocks.slice(start, start + pageSize)) {
    const card = element("article", "", "stock");
    const top = element("div", "", "stock-top");
    const identity = element("div", "", "stock-identity");
    const name = stock.quote?.name || (stock.quote?.refreshing ? "Cargando nombre…" : "Nombre no disponible");
    identity.append(element("h3", stock.ticker), element("p", name, "company-name"));
    top.append(identity, element("span", statuses[stock.status], "badge " + stock.status));
    card.append(top);
    const summary = stock.report_date
      ? "Último informe: " + stock.report_date + " · " + stock.rating
      : "Todavía no has generado un informe.";
    top.append(element("p", summary, "meta"));
    card.append(marketDetails(stock.quote));
    if (stock.error) card.append(element("p", stock.error, "stock-error"));
    const actions = element("div", "", "actions");
    actions.append(actionButton(stock.report_url ? "Actualizar informe" : "Generar informe", "analyze", stock.ticker, data.busy));
    if (stock.report_url) {
      const link = element("a", "Abrir último informe", "report-link");
      link.href = stock.report_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.dataset.action = "report";
      link.dataset.ticker = stock.ticker;
      link.setAttribute("aria-label", "Abrir último informe de " + stock.ticker);
      actions.append(link);
    }
    actions.append(actionButton("Quitar", "remove", stock.ticker, stock.status === "running"));
    card.append(actions);
    fragment.append(card);
  }
  stocks.replaceChildren(fragment);
  if (focus?.action) {
    const control = [...stocks.querySelectorAll("[data-action]")].find(control =>
      control.dataset.action === focus.action && control.dataset.ticker === focus.ticker
    );
    (control || document.querySelector("#watchlist-heading")).focus({preventScroll: true});
  }
}

pagination.addEventListener("click", event => {
  if (!currentData) return;
  if (event.target === previousPage && !previousPage.disabled) currentPage--;
  else if (event.target === nextPage && !nextPage.disabled) currentPage++;
  else return;
  render(currentData);
  const heading = document.querySelector("#watchlist-heading");
  heading.focus({preventScroll: true});
  heading.scrollIntoView({block: "start"});
});

async function refresh() {
  const data = await request("/api/stocks");
  const snapshot = JSON.stringify(data);
  if (snapshot !== lastSnapshot) {
    render(data);
    lastSnapshot = snapshot;
  }
}

async function mutate(action, ticker) {
  if (sending) return false;
  sending = true;
  showError("");
  try {
    await request("/api/" + action, ticker);
    await refresh();
    return true;
  } catch (failure) {
    showError(failure.message);
    return false;
  } finally {
    sending = false;
  }
}

input.addEventListener("input", () => {
  searchVersion++;
  searchResults.replaceChildren();
  searchResults.hidden = true;
  searchStatus.textContent = "";
  form.removeAttribute("aria-busy");
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query) return;
  const version = ++searchVersion;
  showError("");
  searchResults.replaceChildren();
  searchResults.hidden = true;
  searchStatus.textContent = "Buscando acciones…";
  form.setAttribute("aria-busy", "true");
  try {
    const data = await request("/api/search?q=" + encodeURIComponent(query));
    if (version !== searchVersion) return;
    for (const stock of data.results) {
      const row = element("li", "", "search-result");
      const identity = element("div");
      identity.append(element("strong", stock.name), element("span", stock.ticker + " · " + stock.exchange));
      row.append(identity, actionButton("Añadir", "add", stock.ticker, false));
      searchResults.append(row);
    }
    searchResults.hidden = data.results.length === 0;
    searchStatus.textContent = data.results.length
      ? data.results.length + " resultados. Comprueba la empresa y el mercado antes de añadir."
      : "No encontramos esa acción. Prueba otro nombre o símbolo.";
  } catch (failure) {
    if (version === searchVersion) searchStatus.textContent = failure.message;
  } finally {
    if (version === searchVersion) form.removeAttribute("aria-busy");
  }
});

searchResults.addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const version = searchVersion;
  button.disabled = true;
  const added = await mutate("add", button.dataset.ticker);
  button.disabled = added;
  if (added) {
    button.textContent = "Añadida";
    button.setAttribute("aria-label", "Añadida " + button.dataset.ticker);
    if (version === searchVersion) searchStatus.textContent = button.dataset.ticker + " añadida a tu lista. No se ha iniciado ningún análisis.";
  }
});

stocks.addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const {action, ticker} = button.dataset;
  const prompt = action === "analyze"
    ? "¿Generar el informe de " + ticker + " con fecha de hoy? Consumirá llamadas de IA o los límites de tu suscripción."
    : "¿Quitar " + ticker + " de tu lista? Sus informes seguirán guardados en el disco.";
  if (window.confirm(prompt)) await mutate(action, ticker);
});

async function poll() {
  try {
    await refresh();
  } catch {
    document.querySelector("#activity").textContent = "Sin conexión con el panel. Comprueba que el servidor sigue abierto.";
    lastSnapshot = "";
  } finally {
    window.setTimeout(poll, 3000);
  }
}
poll();
