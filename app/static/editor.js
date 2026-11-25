import { api, button, choices, el, explain, idText, message, node, offerSearch } from "./ui.js";

const locked = new Map(Object.entries({
    publishing: "Публикация начата. Сверьте состояние после завершения записи.",
    unknown: "Исход публикации неизвестен. Сверьте состояние через Refresh.",
    verification_failed: "Контрольный GET не подтвердил публикацию. Выполните сверку.",
    reconciliation_conflict: "Состояние отличается от базы и цели. Требуется ручная сверка.",
}));
const rowStates = { active: "Активен", removed: "Удалён из черновика", external_removed: "Удалён извне" };

export function setupEditor() {
    const campaign = el("campaign");
    const stream = el("stream");
    const query = el("editor-query");
    const status = el("editor-status");
    const rows = el("offer-rows");
    const refresh = el("refresh-editor");
    const cancel = el("cancel-editor");
    const push = el("push-editor");
    const reload = el("reload-editor");
    const catalog = el("load-catalog");
    let draft;
    let busy = false;
    let stale = false;
    let generation = 0;
    let catalogLoaded = false;
    let loading = "";
    let discardNeedsRefresh = false;

    const editable = () => draft?.status === "ready" && !busy && !stale && !discardNeedsRefresh;
    const clearSearch = offerSearch("editor", (offer) => {
        query.value = "";
        mutate("actions", {action: "add", offer_id: offer.id}, editable);
    }, editable);

    function addRow(row) {
        const active = row.state === "active";
        const line = node("tr");
        line.toggleAttribute("data-removed", !active);
        line.toggleAttribute("data-added", row.added && active);
        const title = node("th", row.name);
        title.scope = "row";
        title.className = "offer-name";
        title.append(node("small", "#" + row.offer_id));
        const state = [rowStates[row.state], row.pinned && "Доля закреплена",
            row.added && "Новая привязка", !row.available && "Недоступен в каталоге"].filter(Boolean);
        const controls = node("td");
        function action(label, command) {
            const control = button(label, () => mutate("actions", {action: command, offer_id: row.offer_id}, editable));
            control.disabled = !editable();
            control.setAttribute("aria-label", label + " · #" + row.offer_id);
            control.dataset.action = command;
            control.dataset.offer = row.offer_id;
            controls.append(control);
        }
        if (active) {
            action(row.pinned ? "Unpin · Открепить" : "Pin · Закрепить", row.pinned ? "unpin" : "pin");
            action("Удалить", "remove");
        } else if (row.available) {
            action("Bring Back · Вернуть", "restore");
        }
        line.append(title, node("td", row.share + "%"), node("td", state.join(" · ")), controls);
        rows.append(line);
    }

    function render() {
        const focus = document.activeElement?.dataset;
        const active = draft?.rows.filter((r) => r.state === "active") || [];
        const changed = draft?.dirty || draft?.metadata_changed;
        const selected = !!campaign.value && !!stream.value;
        campaign.disabled = busy || loading === "campaigns" || campaign.options.length < 2;
        stream.disabled = busy || loading !== "" && loading !== "draft" || stream.options.length < 2;
        catalog.disabled = busy || loading === "campaigns";
        query.disabled = !editable();
        reload.hidden = !selected;
        reload.disabled = busy || !!loading;
        el("draft").hidden = !draft;
        for (const control of [refresh, cancel, push]) control.hidden = !draft;
        refresh.disabled = !draft || busy || stale;
        cancel.disabled = !draft || busy || stale || locked.has(draft.status)
            || (!changed && draft.status !== "conflict");
        push.disabled = !editable() || !changed || !active.length;
        push.textContent = draft?.metadata_changed && !draft.dirty
            ? "Push to KT · Сохранить закрепления локально" : "Push to KT · Опубликовать";
        if (!draft) return;

        const label = draft.status === "conflict" ? "Конфликт"
            : locked.has(draft.status) ? "Требуется сверка"
            : changed ? "Черновик" : "Синхронизировано с последней принятой базой";
        el("draft-state").textContent = label + " · версия " + draft.revision;
        el("draft-state").parentElement.toggleAttribute("data-dirty", changed || draft.status !== "ready");
        el("draft-total").textContent = "Активные доли: " + active.reduce((sum, item) => sum + item.share, 0) + "%";
        const notes = [];
        if (locked.has(draft.status)) notes.push(locked.get(draft.status), "Стадия: " + draft.stage + ".");
        if (draft.status === "conflict") notes.push("Для принятия удалённых изменений выберите Cancel, затем Refresh.");
        if (discardNeedsRefresh) notes.push("Правки отменены. Выполните Refresh для принятия удалённого состояния.");
        if (draft.manual_check_required) notes.push("На момент GET наблюдалась исходная база. Возможен поздний эффект предыдущего PUT. Перед новой публикацией выполните ручную сверку.");
        if (!active.length) notes.push("Активных привязок нет. Публикация пустого состава пока недоступна.");
        for (const [key, label] of [["added", "Добавлены"], ["removed", "Удалены"], ["changed", "Изменены"]]) {
            if (draft.changes[key].length) notes.push(label + " ID: " + draft.changes[key].join(", ") + ".");
        }
        if (draft.changes.stream_changed) notes.push("Изменились другие настройки потока.");
        message(el("draft-note"), notes.join(" "));
        rows.replaceChildren();
        draft.rows.forEach(addRow);
        if (!draft.rows.length) {
            const line = node("tr");
            const cell = node("td", "Привязок пока нет");
            cell.colSpan = 4;
            line.append(cell);
            rows.append(line);
        }
        if (focus?.action && focus?.offer) {
            rows.querySelector('[data-action="' + focus.action + '"][data-offer="' + focus.offer + '"]')?.focus();
        }
    }

    function accept(value) {
        if (!value || !Array.isArray(value.rows) || !value.changes
            || !["ready", "conflict", ...locked.keys()].includes(value.status)) {
            throw new Error("invalid_response");
        }
        if (idText(value.campaign_id) !== campaign.value || idText(value.stream_id) !== stream.value) {
            throw new Error("invalid_response");
        }
        idText(value.revision);
        for (const row of value.rows) idText(row.offer_id);
        for (const key of ["added", "removed", "changed"]) value.changes[key].forEach(idText);
        draft = value;
        stale = false;
    }

    function path() {
        return "/api/campaigns/" + idText(Number(campaign.value)) + "/streams/" + idText(Number(stream.value)) + "/editor";
    }

    function options(select, title, values = [], previous = "") {
        const entries = values.map((item) => new Option(item.name + " · #" + item.id, String(item.id)));
        select.replaceChildren(new Option(title, ""), ...entries);
        if (values.some((item) => String(item.id) === previous)) select.value = previous;
    }

    function invalidate() {
        clearSearch();
        query.value = "";
        generation++;
        loading = "";
        draft = undefined;
        stale = false;
        discardNeedsRefresh = false;
    }

    async function read(kind, url, done) {
        const current = ++generation;
        loading = kind;
        message(status, "Загрузка…");
        render();
        try {
            const data = await api(url);
            if (current !== generation) return;
            loading = "";
            message(status, "");
            done(data);
        } catch (error) {
            if (current !== generation) return;
            loading = "";
            message(status, explain(error), true);
        }
        render();
    }

    async function loadDraft() {
        if (busy || !campaign.value || !stream.value) return;
        clearSearch();
        stale = true;
        await read("draft", path(), accept);
    }

    function loadStreams(preferred = "") {
        if (busy) return;
        invalidate();
        options(stream, "Выберите поток");
        if (!campaign.value) {
            message(status, "");
            render();
            return;
        }
        read("streams", "/api/campaigns/" + idText(Number(campaign.value)) + "/streams", (data) => {
            const items = choices(data);
            options(stream, "Выберите поток", items, preferred);
            if (stream.value) loadDraft();
            else if (!items.length) message(status, "Подходящие потоки не найдены.");
        });
    }

    function loadCatalog() {
        if (busy) return;
        const previous = campaign.value;
        const previousStream = stream.value;
        invalidate();
        options(campaign, "Выберите кампанию");
        options(stream, "Выберите поток");
        read("campaigns", "/api/campaigns", (data) => {
            catalogLoaded = true;
            const items = choices(data);
            options(campaign, "Выберите кампанию", items, previous);
            if (campaign.value) loadStreams(previousStream);
            else if (!items.length) message(status, "Кампании этого проекта не найдены.");
        });
    }

    async function mutate(verb, fields, allowed) {
        if (busy || loading || stale || !draft || !allowed()) return;
        const before = draft;
        const focused = document.activeElement;
        const focusAction = focused?.dataset.action;
        const focusOffer = focused?.dataset.offer;
        busy = true;
        clearSearch();
        message(status, "Запрос выполняется…");
        render();
        try {
            accept(await api(path() + "/" + verb, {body: {expected_revision: before.revision, ...fields}}));
            if (verb === "cancel" && before.status === "conflict") discardNeedsRefresh = true;
            if (verb === "refresh") discardNeedsRefresh = false;
            if (verb === "push" && draft.status === "ready" && !draft.dirty && !draft.metadata_changed) {
                message(status, before.dirty ? "Публикация подтверждена контрольным чтением." : "Закрепления сохранены локально.");
            } else {
                message(status, "");
            }
        } catch (error) {
            try {
                accept(error.data?.editor);
            } catch (invalid) {
                stale = true;
                message(status, explain(error) + " " + (error.data?.editor ? explain(invalid) + " " : "") + "Перечитайте черновик и при необходимости выполните Refresh.", true);
            }
            if (!stale) message(status, explain(error), true);
        } finally {
            busy = false;
            render();
            if (focusAction && focusOffer) {
                const same = rows.querySelector('[data-action="' + focusAction + '"][data-offer="' + focusOffer + '"]');
                (same && !same.disabled ? same : stale ? reload : editable() ? query : refresh).focus();
            }
        }
    }

    catalog.addEventListener("click", loadCatalog);
    campaign.addEventListener("change", () => loadStreams());
    stream.addEventListener("change", () => {
        invalidate();
        if (stream.value) loadDraft();
        else {
            message(status, "");
            render();
        }
    });
    reload.addEventListener("click", loadDraft);
    refresh.addEventListener("click", () => mutate("refresh", {}, () => !refresh.disabled));
    cancel.addEventListener("click", () => {
        if (!cancel.disabled && window.confirm("Отменить локальные правки? Опубликованные настройки в Keitaro сохранятся.")) {
            mutate("cancel", {}, () => !cancel.disabled);
        }
    });
    push.addEventListener("click", () => {
        if (push.disabled) return;
        const acknowledge = draft.manual_check_required && draft.dirty;
        if (acknowledge
            && !window.confirm("Подтвердите ручную сверку предыдущей записи перед новой публикацией.")) return;
        mutate("push", acknowledge ? {acknowledge_manual_check: true} : {}, () => !push.disabled);
    });
    render();
    return () => { if (!catalogLoaded && !loading && !busy) loadCatalog(); };
}
