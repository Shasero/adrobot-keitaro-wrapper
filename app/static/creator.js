import { api, el, explain, message, node, safeId, offerSearch } from "./ui.js";

const KEY = "adrobot.campaign-operation.v1";
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const instructions = new Map(Object.entries({
    succeeded: "Создание подтверждено. Кампанию можно выбрать в редакторе, обновив список.",
    running: "Создание выполняется. Проверьте статус по тому же UUID.",
    failed: "Создание завершилось ошибкой. Устраните указанную причину перед новой операцией.",
    partial: "Создание подтверждено частично. Сверьте объекты по alias и известным ID.",
    unknown: "Результат записи неизвестен. Проверьте статус и сверьте объекты по alias и известным ID.",
}));

export function setupCreator() {
    const name = el("campaign-name");
    const geo = el("geo");
    const query = el("create-query");
    const status = el("create-status");
    const details = el("operation");
    let selected;
    let intent;
    let result;
    let busy = false;
    let storageAvailable = true;
    let uncertain = true;
    const canLock = typeof navigator.locks?.request === "function";

    async function withReservation(action) {
        if (!canLock || busy) return;
        busy = true;
        render();
        try {
            await navigator.locks.request(KEY, {ifAvailable: true}, async (lock) => {
                if (lock) await action();
                else message(status, "Другая вкладка работает с операцией. Дождитесь завершения и перечитайте страницу.", true);
            });
        } catch {
            storageAvailable = false;
            message(status, "Блокировка или хранилище недоступны. Перечитайте операцию после перезагрузки.", true);
        } finally {
            busy = false;
            render();
        }
    }

    function render() {
        el("create-fields").disabled = !storageAvailable || !canLock || !!intent || busy;
        el("create-submit").disabled = !storageAvailable || !canLock || !!intent || busy || !selected;
        el("check-operation").hidden = !intent;
        el("new-operation").hidden = !intent;
        el("check-operation").disabled = busy;
        el("new-operation").disabled = busy || !canLock || !storageAvailable;
        el("selected-offer").textContent = selected
            ? "Выбран оффер " + selected.name + " · #" + selected.id
            : intent ? "Оффер #" + intent.offer_id : "Оффер не выбран";
    }

    function display(operation, problem) {
        result = operation;
        uncertain = !operation || !!operation.reconciliation_required || problem?.code === "storage";
        const guidance = instructions.get(operation?.status)
            || "Сверьте операцию по UUID. При обрыве новый UUID может создать дубликат.";
        message(status, [problem && explain(problem), operation?.error_code && explain({code: operation.error_code}),
            guidance].filter(Boolean).filter((item, i, items) => items.indexOf(item) === i).join(" "),
        operation?.status !== "succeeded");
        details.hidden = false;
        details.replaceChildren();
        function detail(label, value) {
            if (value != null) details.append(node("dt", label), node("dd", value));
        }
        function remoteId(label, value) {
            if (value != null) detail(label, safeId(value) ? value : "ID вне безопасного диапазона");
        }
        detail("UUID операции", intent.operation_id);
        detail("Статус", operation?.status);
        detail("Стадия", operation?.stage);
        detail("Alias", operation?.alias);
        remoteId("Кампания", operation?.remote_campaign_id);
        remoteId("Обычный поток", operation?.regular_stream_id);
        remoteId("Замыкающий поток", operation?.default_stream_id);
        remoteId("Полученный ID кампании, сохранение не подтверждено", problem?.data?.observed_ids?.campaign_id);
        remoteId("Полученный ID обычного потока, сохранение не подтверждено", problem?.data?.observed_ids?.regular_id);
        remoteId("Полученный ID замыкающего потока, сохранение не подтверждено", problem?.data?.observed_ids?.default_id);
    }

    const clearSearch = offerSearch("create", (offer) => {
        selected = offer;
        query.focus();
        render();
    }, () => !intent && !busy && storageAvailable, () => {
        selected = undefined;
        render();
    });

    async function execute(creating) {
        message(status, creating ? "Создание кампании…" : "Проверка статуса…");
        try {
            const operation = creating ? await api("/api/campaigns", { body: intent })
                : await api("/api/campaigns/operations/" + intent.operation_id);
            if (operation.operation_id !== intent.operation_id || !instructions.has(operation.status)) {
                throw new Error("invalid_response");
            }
            display(operation);
        } catch (error) {
            const operation = error.data?.operation;
            display(operation?.operation_id === intent.operation_id ? operation : undefined, error);
        }
    }

    try {
        const stored = JSON.parse(localStorage.getItem(KEY));
        if (stored !== null) {
            if (typeof stored.operation_id !== "string" || !UUID.test(stored.operation_id) || !safeId(stored.offer_id)
                || typeof stored.name !== "string" || !/^[A-Za-z]{2}$/.test(stored.geo)) {
                throw new Error("invalid_response");
            }
            intent = {name: stored.name, geo: stored.geo, offer_id: stored.offer_id,
                operation_id: stored.operation_id};
            name.value = intent.name;
            geo.value = intent.geo;
            display();
        }
    } catch {
        storageAvailable = false;
        message(status, "Хранилище браузера недоступно или повреждено. Создание отключено, так как UUID нельзя восстановить после перезагрузки.", true);
    }
    if (!canLock) message(status, "Web Locks недоступен. Создание и сброс операции отключены.", true);
    render();

    el("create-form").addEventListener("submit", (event) => {
        event.preventDefault();
        if (!storageAvailable || intent || busy || !selected) return;
        if (!name.value.trim()) {
            message(status, "Введите название кампании.", true);
            name.focus();
            return;
        }
        withReservation(async () => {
            if (localStorage.getItem(KEY) !== null) {
                message(status, "Другая вкладка сохранила операцию. Перезагрузите страницу для сверки.", true);
                return;
            }
            const pending = { name: name.value.trim(), geo: geo.value.toUpperCase(),
                offer_id: selected.id, operation_id: crypto.randomUUID() };
            localStorage.setItem(KEY, JSON.stringify(pending));
            intent = pending;
            clearSearch();
            await execute(true);
        });
    });

    el("check-operation").addEventListener("click", async () => {
        if (busy || !intent) return;
        busy = true;
        render();
        try {
            await execute(false);
        } finally {
            busy = false;
            render();
        }
    });
    el("new-operation").addEventListener("click", () => {
        if (busy || !intent) return;
        const risk = uncertain || !result || ["running", "partial", "unknown"].includes(result.status);
        if (risk && !window.confirm("Новый UUID может создать дубликат. Сначала сверьте предыдущую операцию по UUID, alias и известным ID. Сверка выполнена?")) return;
        withReservation(() => {
            const current = JSON.parse(localStorage.getItem(KEY));
            if (current?.operation_id !== intent.operation_id) {
                message(status, "Сохранённая операция изменилась. Перечитайте страницу.", true);
                return;
            }
            localStorage.removeItem(KEY);
            intent = result = selected = undefined;
            uncertain = true;
            el("create-form").reset();
            details.hidden = true;
            details.replaceChildren();
            message(status, "");
            render();
        }).then(() => { if (!intent) name.focus(); });
    });
}
