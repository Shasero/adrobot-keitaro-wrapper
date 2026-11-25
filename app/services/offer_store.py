"""Short database transactions and a crash-released interprocess publication lock."""
import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import ValidationError, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import OfferDraft
from app.services.offer_shares import Binding, Difference, EditorError, Row, StrictModel, bindings, metadata

Status = Literal["ready", "conflict", "publishing", "unknown", "verification_failed", "reconciliation_conflict"]


class Document(StrictModel):
    saved: list[Row]
    rows: list[Row]
    fingerprint: str
    status: Status = "ready"
    stage: Literal["idle", "preflight", "put", "verify"] = "idle"
    error_code: str | None = None
    changes: Difference = Difference()
    target: list[Binding] | None = None

    @model_validator(mode="after")
    def consistent(self):
        for rows in (self.saved, self.rows):
            keys = [r.offer_id for r in rows]
            if keys != sorted(set(keys)):
                raise ValueError("Invalid row identifiers")
            for row in rows:
                if row.state == "active" and not row.share:
                    raise ValueError("Active offer without share")
                if row.state != "active" and (row.share or row.pinned or row.added):
                    raise ValueError("Invalid tombstone")
            active = bindings(rows)
            if active and sum(b.share for b in active) != 100:
                raise ValueError("Invalid share total")
        if self.locked and self.target != bindings(self.rows):
            raise ValueError("Invalid publication intent")
        return self

    @property
    def dirty(self) -> bool:
        return bindings(self.saved) != bindings(self.rows)

    @property
    def metadata_changed(self) -> bool:
        return metadata(self.saved) != metadata(self.rows)

    @property
    def locked(self) -> bool:
        return self.status not in ("ready", "conflict")


class Store:
    def __init__(self, factory: sessionmaker, campaign_id: int, stream_id: int):
        self.factory = factory
        self.key = (campaign_id, stream_id)

    def read(self) -> tuple[int, Document] | None:
        with self.factory() as db:
            row = db.get(OfferDraft, self.key)
            if row is None:
                return None
            try:
                return row.revision, Document.model_validate(row.document)
            except ValidationError:
                raise EditorError("storage") from None

    def insert(self, doc: Document) -> tuple[int, Document]:
        try:
            with self.factory.begin() as db:
                db.add(OfferDraft(campaign_id=self.key[0], stream_id=self.key[1],
                                  revision=1, document=doc.model_dump()))
            return 1, doc
        except IntegrityError:
            existing = self.read()
            if existing is None:
                raise
            return existing

    def update(self, revision: int, doc: Document) -> int:
        with self.factory.begin() as db:
            result = db.execute(update(OfferDraft).where(
                OfferDraft.campaign_id == self.key[0], OfferDraft.stream_id == self.key[1],
                OfferDraft.revision == revision,
            ).values(revision=revision + 1, document=doc.model_dump()))
            if result.rowcount != 1:
                raise EditorError("stale_revision")
        return revision + 1

    @contextmanager
    def publication_lock(self):
        # CAS protects the draft. The OS lock keeps explicit recovery from
        # declaring a live PUT absent. The OS releases it on process death.
        with self.factory() as db:
            database = db.get_bind().url.database
        if not database or database == ":memory:":
            raise EditorError("storage")
        folder = Path(database).resolve().parent / ".adrobot-editor-locks"
        filename = f"{Path(database).name}.{self.key[0]}.{self.key[1]}.lock"
        try:
            folder.mkdir(exist_ok=True)
            handle = (folder / filename).open("a+b")
        except OSError:
            raise EditorError("storage") from None
        with handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise EditorError("busy") from None
                raise EditorError("storage") from None
            yield
