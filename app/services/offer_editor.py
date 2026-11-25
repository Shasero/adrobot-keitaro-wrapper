from app.integrations.keitaro import KeitaroClient, KeitaroError
from app.services.campaigns import CampaignError, offers_by_id, upstream_code, valid_id
from app.services.offer_shares import (
    Action, Difference, EditorError, Row, StrictModel, apply_action, bindings, describe,
    difference, import_rows,
)
from app.services.offer_store import Document, Status, Store
from app.services.offer_streams import Snapshot, snapshot


class EditorView(StrictModel):
    campaign_id: int
    stream_id: int
    revision: int
    rows: list[Row]
    dirty: bool
    metadata_changed: bool
    status: Status
    stage: str
    error_code: str | None
    manual_check_required: bool
    outcome_unknown: bool
    changes: Difference


def error_code(exc: Exception) -> str:
    if isinstance(exc, KeitaroError):
        return "forbidden" if exc.category == "write_forbidden" else upstream_code(exc)
    return exc.code


class Editor:
    def __init__(self, store: Store, client: KeitaroClient):
        self.store = store
        self.client = client

    def view(self, revision: int, doc: Document) -> EditorView:
        return EditorView(campaign_id=self.store.key[0], stream_id=self.store.key[1],
                          revision=revision, rows=doc.rows, dirty=doc.dirty,
                          metadata_changed=doc.metadata_changed, status=doc.status,
                          stage=doc.stage, error_code=doc.error_code, changes=doc.changes,
                          manual_check_required=doc.manual_check_required,
                          outcome_unknown=doc.locked and doc.stage in ("put", "verify"))

    def remote(self) -> Snapshot:
        return snapshot(self.client.checked_stream(*self.store.key), *self.store.key)

    def open(self) -> EditorView:
        stream = self.client.checked_stream(*self.store.key)
        if not valid_id(stream.get("campaign_id")) or stream["campaign_id"] != self.store.key[0]:
            raise EditorError("forbidden")
        stored = self.store.read()
        if stored is None or not stored[1].locked:
            remote = snapshot(stream, *self.store.key)
        if stored is None:
            rows = import_rows([], remote.offers, offers_by_id(self.client))
            stored = self.store.insert(Document(saved=rows, rows=rows, fingerprint=remote.fingerprint))
        return self.view(*stored)

    def current(self, revision: int, *, allow_locked: bool = False) -> Document:
        stored = self.store.read()
        if stored is None:
            raise EditorError("not_found")
        actual, doc = stored
        if revision != actual:
            raise EditorError("stale_revision")
        if doc.locked and not allow_locked:
            raise EditorError("busy")
        return doc

    def save(self, revision: int, doc: Document) -> EditorView:
        return self.view(self.store.update(revision, doc), doc)

    def action(self, revision: int, action: Action, key: int) -> EditorView:
        doc = self.current(revision)
        if doc.status == "conflict":
            raise EditorError("conflict")
        self.remote()
        catalog = offers_by_id(self.client) if action in ("add", "restore") else {}
        rows = apply_action(doc.rows, action, key, catalog, {b.offer_id for b in bindings(doc.saved)})
        if rows == doc.rows:
            return self.view(revision, self.current(revision))
        doc.rows, doc.error_code = rows, None
        return self.save(revision, doc)

    def cancel(self, revision: int) -> EditorView:
        doc = self.current(revision)
        self.client.checked_stream(*self.store.key)
        doc.rows = doc.saved
        doc.status, doc.error_code, doc.changes = "ready", None, Difference()
        return self.save(revision, doc)

    def refresh(self, revision: int) -> EditorView:
        with self.store.publication_lock():
            doc = self.current(revision, allow_locked=True)
            try:
                remote = self.remote()
            except EditorError as exc:
                if not doc.locked or exc.code not in ("unsupported_stream", "unsupported_bindings", "protocol"):
                    raise
                doc.status, doc.error_code = "reconciliation_conflict", exc.code
                return self.save(revision, doc)
            catalog = offers_by_id(self.client)
            base = bindings(doc.saved)
            changes = difference(base, remote.offers)
            changes.stream_changed = doc.fingerprint != remote.fingerprint
            if doc.locked:
                if not changes.stream_changed and remote.offers == doc.target:
                    self.accept(doc)
                elif not changes.stream_changed and remote.offers == base:
                    observed_write = doc.stage in ("put", "verify")
                    doc.status, doc.stage, doc.target = "ready", "idle", None
                    doc.error_code = "base_confirmed" if observed_write else None
                    doc.manual_check_required |= observed_write
                else:
                    doc.status, doc.error_code = "reconciliation_conflict", "conflict"
            elif remote.offers != base or changes.stream_changed:
                if doc.dirty or doc.metadata_changed:
                    doc.status, doc.error_code = "conflict", "conflict"
                else:
                    doc.saved = doc.rows = import_rows(doc.saved, remote.offers, catalog)
                    doc.fingerprint = remote.fingerprint
                    doc.status, doc.error_code = "ready", None
            else:
                doc.status, doc.error_code = "ready", None
            describe(doc.rows, catalog)
            describe(doc.saved, catalog)
            doc.changes = changes
            return self.save(revision, doc)

    @staticmethod
    def accept(doc: Document) -> None:
        for row in doc.rows:
            row.added = False
        doc.saved = doc.rows
        doc.status, doc.stage, doc.error_code = "ready", "idle", None
        doc.target, doc.changes = None, Difference()

    def push(self, revision: int, acknowledge_manual_check: bool = False) -> EditorView:
        with self.store.publication_lock():
            doc = self.current(revision)
            if doc.status == "conflict":
                raise EditorError("conflict")
            if doc.dirty and doc.manual_check_required and not acknowledge_manual_check:
                self.remote()  # Check ownership before returning stored draft details.
                error = EditorError("manual_check_required")
                error.editor = self.view(revision, doc)
                raise error
            target = bindings(doc.rows)
            doc.status, doc.stage, doc.target = "publishing", "preflight", target
            doc.error_code = None
            revision = self.store.update(revision, doc)

            def compare(remote: Snapshot) -> None:
                doc.changes = difference(bindings(doc.saved), remote.offers)
                doc.changes.stream_changed = remote.fingerprint != doc.fingerprint
                if remote.offers != bindings(doc.saved) or doc.changes.stream_changed:
                    raise EditorError("conflict")

            def before_put(record: dict) -> None:
                nonlocal revision
                compare(snapshot(record, *self.store.key))
                doc.stage = "put"
                revision = self.store.update(revision, doc)

            try:
                compare(self.remote())
                if not target:
                    raise EditorError("empty_target")
                catalog = offers_by_id(self.client)
                if doc.dirty and any(catalog.get(b.offer_id, {}).get("state") != "active" for b in target):
                    raise EditorError("offer_unavailable")
                describe(doc.rows, catalog)
                if doc.dirty:
                    reply = self.client.update_stream_offers(
                        *self.store.key, [b.model_dump() for b in target], before_put=before_put)
                    if not valid_id(reply.get("id")) or reply["id"] != self.store.key[1]:
                        raise KeitaroError("protocol", outcome_unknown=True)
                    doc.stage = "verify"
                    revision = self.store.update(revision, doc)
                    after = snapshot(self.client.get_stream(self.store.key[1]), *self.store.key)
                    if after.offers != target or after.fingerprint != doc.fingerprint:
                        raise EditorError("verification")
                    doc.manual_check_required = False
            except (CampaignError, KeitaroError, EditorError) as exc:
                code = error_code(exc)
                if code in ("storage", "stale_revision"):
                    raise
                if doc.stage == "verify":
                    doc.status = "verification_failed"
                elif isinstance(exc, KeitaroError) and exc.outcome_unknown:
                    doc.status = "unknown"
                else:
                    doc.status = "conflict" if code == "conflict" else "ready"
                    doc.target, doc.stage = None, "idle"
                doc.error_code = code
                result = self.save(revision, doc)
                error = EditorError(code)
                if code != "forbidden":
                    error.editor = result
                raise error from None
            self.accept(doc)
            return self.save(revision, doc)
