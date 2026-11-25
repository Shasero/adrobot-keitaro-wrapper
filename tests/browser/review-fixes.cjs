// Optional Chromium regression: run a local AdRobot on port 8771, then
// NODE_PATH=<directory containing playwright> CHROMIUM=<binary> node tests/browser/review-fixes.cjs
const assert = require("node:assert/strict");
const {chromium} = require("playwright");

async function run(browser) {
    const origin = "http://127.0.0.1:8771";
    const key = "adrobot.campaign-operation.v1";
    const context = await browser.newContext();
    let releasePost;
    const held = new Promise(resolve => { releasePost = resolve; });
    const creates = [];
    const lookups = [];
    const pushes = [];
    const view = {
        campaign_id: 1, stream_id: 2, revision: 1, status: "ready", stage: "idle",
        manual_check_required: true, dirty: true, metadata_changed: false,
        changes: {added: [], removed: [], changed: [], stream_changed: false},
        rows: [{offer_id: 4, name: "Fixture", share: 100, state: "active", available: true}],
    };
    await context.route("**/*", async route => {
        const url = new URL(route.request().url());
        if (url.origin !== origin) return route.abort();
        const path = url.pathname;
        if (!path.startsWith("/api/")) return route.continue();
        const send = json => route.fulfill({json});
        if (path === "/api/offers") return send([{id: 4, name: "Fixture"}]);
        if (path === "/api/campaigns" && route.request().method() === "POST") {
            const body = route.request().postDataJSON();
            creates.push(body.operation_id);
            await held;
            return send({operation_id: body.operation_id, status: "running"});
        }
        if (path.startsWith("/api/campaigns/operations/")) {
            lookups.push(path);
            return send({operation_id: creates[0], status: "running"});
        }
        if (path === "/api/campaigns") return send([{id: 1, name: "Campaign"}]);
        if (path.endsWith("/streams")) return send([{id: 2, name: "Stream"}]);
        if (path.endsWith("/actions")) {
            view.error_code = null;
            view.metadata_changed = true;
            view.revision++;
            return send(view);
        }
        if (path.endsWith("/push")) {
            pushes.push(route.request().postDataJSON());
            return send({...view, manual_check_required: false, dirty: false, metadata_changed: false});
        }
        assert(path.endsWith("/editor"));
        return send(view);
    });

    const tabs = await Promise.all([context.newPage(), context.newPage()]);
    await Promise.all(tabs.map(async page => {
        page.on("dialog", dialog => dialog.accept());
        await page.goto(origin);
        await page.locator("#campaign-name").fill("Demo");
        await page.locator("#geo").fill("US");
        await page.locator("#create-query").fill("Fixture");
        await page.locator("#create-results button").click();
    }));
    await Promise.all(tabs.map(page => page.locator("#create-submit").evaluate(b => { b.click(); b.click(); })));
    const observer = await context.newPage();
    observer.on("dialog", dialog => dialog.accept());
    await observer.goto(origin);
    await observer.locator("#new-operation").click();
    await observer.getByText("Другая вкладка работает", {exact: false}).waitFor();
    const reserved = page => page.evaluate(k => JSON.parse(localStorage.getItem(k))?.operation_id, key);
    assert.equal(creates.length, 1);
    for (const page of tabs) assert.equal(await reserved(page), creates[0]);
    releasePost();
    await Promise.race(tabs.map(page => page.locator("#create-status").getByText("Создание выполняется", {exact: false}).waitFor()));
    await observer.reload();
    await observer.locator("#check-operation").click();
    await observer.locator("#create-status").getByText("Создание выполняется", {exact: false}).waitFor();
    assert.deepEqual(lookups, ["/api/campaigns/operations/" + creates[0]]);
    await observer.locator("#new-operation").click();
    await observer.waitForFunction(k => localStorage.getItem(k) === null, key);
    assert.equal(creates.length, 1);

    for (const property of ["locks", "localStorage"]) {
        const page = await context.newPage();
        await page.addInitScript(prop => {
            Object.defineProperty(prop === "locks" ? navigator : window, prop, {get() {
                if (prop === "locks") return undefined;
                throw new Error("unavailable");
            }});
        }, property);
        await page.goto(origin);
        await page.locator("#create-status").getByText(/недоступ/).waitFor();
        assert(await page.locator("#campaign-name").isDisabled());
        await page.close();
    }
    const page = await context.newPage();
    async function open() {
        await page.goto(origin);
        await page.locator("#show-editor").click();
        await page.locator("#campaign").selectOption("1");
        await page.locator("#stream").selectOption("2");
        await page.locator("#draft-note").getByText(/ручную сверку/).waitFor();
    }
    await open();
    await page.locator('[data-action="pin"]').click();
    await page.locator("#draft-state").getByText(/версия 2/).waitFor();
    await open();
    page.once("dialog", dialog => dialog.dismiss());
    await page.locator("#push-editor").click();
    assert.equal(pushes.length, 0);
    page.once("dialog", dialog => dialog.accept());
    await page.locator("#push-editor").click();
    await page.getByText("Публикация подтверждена контрольным чтением.").waitFor();
    assert.deepEqual(pushes, [{expected_revision: 2, acknowledge_manual_check: true}]);
    console.log("Two tabs, one UUID and POST; reset, recovery, unavailable APIs and manual acknowledgement passed.");
}

(async () => {
    const browser = await chromium.launch({executablePath: process.env.CHROMIUM});
    try { await run(browser); } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
