async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const makeStocks = count => Array.from({length: count}, (_, index) => ({
    ticker: "TEST" + String(index + 1).padStart(2, "0"), status: "idle",
    quote: {name: "Example Company " + (index + 1), price: 100, currency: "USD"},
  }));
  const data = {
    stocks: makeStocks(24), busy: false,
    models: {quick: {provider: "test", model: "test"}, deep: {provider: "test", model: "test"}},
  };
  await page.route("**/api/stocks", route => route.fulfill({json: data}));
  await page.route("**/api/remove", async route => {
    const {ticker} = route.request().postDataJSON();
    data.stocks = data.stocks.filter(stock => stock.ticker !== ticker);
    await route.fulfill({json: {ok: true}});
  });
  await page.addInitScript(() => { window.confirm = () => true; });
  const checkPage = async (count, first, status) => {
    await page.waitForFunction(expected => document.querySelector("#page-status").textContent === expected, status);
    assert(await page.locator("#stocks .stock").count() === count, "Wrong page size");
    assert(await page.locator("#stocks h3").first().textContent() === first, "Wrong first symbol");
  };
  try {
    await page.goto("http://127.0.0.1:8765");
    await checkPage(10, "TEST01", "1–10 de 24 · Página 1 de 3");
    assert(await page.locator("#previous-page").isDisabled(), "Previous enabled on first page");
    await page.locator("#next-page").click();
    await checkPage(10, "TEST11", "11–20 de 24 · Página 2 de 3");
    assert(await page.locator("#watchlist-heading").evaluate(node => node === document.activeElement), "Page heading did not receive focus");
    data.stocks[10].quote.price = 432.1;
    await page.waitForFunction(() => document.querySelector("#stocks .price").textContent === "432,10 USD");
    await checkPage(10, "TEST11", "11–20 de 24 · Página 2 de 3");
    await page.locator("#next-page").click();
    await checkPage(4, "TEST21", "21–24 de 24 · Página 3 de 3");
    assert(await page.locator("#next-page").isDisabled(), "Next enabled on last page");
    await page.locator("#previous-page").click();
    await checkPage(10, "TEST11", "11–20 de 24 · Página 2 de 3");
    for (const width of [320, 390, 1280]) {
      await page.setViewportSize({width, height: 844});
      assert(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), "Horizontal overflow at " + width);
    }
    data.stocks = makeStocks(11);
    await page.reload();
    await checkPage(10, "TEST01", "1–10 de 11 · Página 1 de 2");
    await page.locator("#next-page").click();
    await checkPage(1, "TEST11", "11–11 de 11 · Página 2 de 2");
    await page.getByRole("button", {name: "Quitar TEST11", exact: true}).click();
    await checkPage(10, "TEST01", "1–10 de 10 · Página 1 de 1");
    assert(await page.locator("#pagination").isHidden(), "Pagination visible for one page");
    assert(await page.locator("#watchlist-heading").evaluate(node => node === document.activeElement), "Focus lost on removal");
    data.stocks = [];
    await page.waitForFunction(() => document.querySelector("#count").textContent === "0");
    assert(await page.locator("#empty").isVisible(), "Empty state missing");
    assert(await page.locator("#pagination").isHidden(), "Pagination visible for empty list");
    assert(await page.locator("#stocks .stock").count() === 0, "Old rows remain");
    return "PASS: page boundaries, quote refresh, removal, focus, empty state, mobile and desktop";
  } finally {
    await page.unroute("**/api/stocks");
    await page.unroute("**/api/remove");
  }
}
