export const el = (id) => document.getElementById(id);
export const safeId = (value) => Number.isSafeInteger(value) && value > 0;
export const idText = (value) => {
    if (!safeId(value)) throw new Error("unsafe_id");
    return String(value);
};

const messages = new Map(Object.entries({
    unsafe_id: "Сервер вернул ID или версию вне безопасного диапазона браузера.",
    configuration: "Интеграция не настроена. Проверьте настройки на сервере.",
    invalid_input: "Проверьте введённые параметры.",
    offer_unavailable: "Оффер отсутствует или выключен. Проверьте каталог.",
    storage: "Сервер не подтвердил локальное сохранение.",
    upstream: "Keitaro не подтвердил запрос.",
    timeout: "Истекло время ожидания ответа Keitaro.",
    protocol: "Keitaro вернул непригодный ответ.",
    verification: "Контрольное чтение не подтвердило ожидаемые настройки.",
    stale_revision: "Версия изменилась. Перечитайте черновик.",
    manual_check_required: "Подтвердите ручную сверку перед публикацией.",
    busy: "Операция выполняется или требует сверки.",
    forbidden: "Кампания или поток недоступны для этого проекта.",
    conflict: "Удалённое состояние изменилось.",
    operation_conflict: "UUID уже связан с другими параметрами. Проверьте статус.",
    unsupported_stream: "Этот поток не поддерживается редактором.",
    unsupported_bindings: "Полученные привязки не поддерживаются редактором.",
    empty_target: "Публикация пустого состава недоступна.",
    share_budget: "Закреплённые доли не оставляют допустимого распределения.",
    invalid_action: "Действие недоступно для этой строки.",
    not_found: "Запись не найдена.",
    invalid_response: "Приложение вернуло непригодный ответ.",
}));

export function explain(error) {
    return messages.get(error.code || error.message) || "Не удалось получить подтверждённый ответ.";
}

export function message(node, text, error = false) {
    node.textContent = text;
    node.toggleAttribute("data-error", error);
}

export function node(tag, content = "") {
    const result = document.createElement(tag);
    result.textContent = content;
    return result;
}

export function button(text, action) {
    const result = node("button", text);
    result.type = "button";
    result.addEventListener("click", action);
    return result;
}

export async function api(path, { body, signal } = {}) {
    if (!path.startsWith("/api/")) throw new Error("invalid_response");
    const response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        credentials: "same-origin",
        mode: "same-origin",
        redirect: "error",
        cache: "no-store",
        signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok || !data || typeof data !== "object") {
        const error = new Error("invalid_response");
        error.code = data?.code;
        error.data = data;
        error.status = response.status;
        throw error;
    }
    return data;
}

export function choices(rows) {
    if (!Array.isArray(rows)) throw new Error("invalid_response");
    for (const row of rows) {
        idText(row?.id);
        if (typeof row.name !== "string") throw new Error("invalid_response");
    }
    return rows;
}

export function offerSearch(scope, onChoose, enabled, onInput = () => {}) {
    const input = el(scope + "-query");
    const list = el(scope + "-results");
    const status = el(scope + "-search-status");
    let timer;
    let request;
    let generation = 0;

    function clear() {
        generation++;
        clearTimeout(timer);
        request?.abort();
        list.replaceChildren();
        message(status, "");
    }

    input.addEventListener("input", () => {
        clear();
        onInput();
        const query = input.value.trim();
        if (!query || !enabled()) return;
        const current = generation;
        message(status, "Поиск офферов…");
        timer = setTimeout(async () => {
            request = new AbortController();
            try {
                const params = new URLSearchParams({ q: query, limit: "20" });
                const offers = choices(await api("/api/offers?" + params, { signal: request.signal }));
                if (current !== generation || !enabled()) return;
                for (const offer of offers) {
                    const item = node("li");
                    item.append(button(offer.name + " · #" + offer.id, () => {
                        if (current !== generation || !enabled()) return;
                        clear();
                        onChoose(offer);
                    }));
                    list.append(item);
                }
                message(status, offers.length ? "Выберите оффер из результатов." : "Активные офферы не найдены.");
            } catch (error) {
                if (current === generation) message(status, explain(error), true);
            }
        }, 300);
    });
    return clear;
}
