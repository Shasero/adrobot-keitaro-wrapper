"""Pure editor rules. Bindings are sorted by offer ID throughout the draft."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.campaigns import MAX_ID

PositiveID = Annotated[int, Field(strict=True, gt=0, le=MAX_ID)]
Percent = Annotated[int, Field(strict=True, ge=0, le=100)]
Action = Literal["add", "remove", "restore", "pin", "unpin"]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Binding(StrictModel):
    offer_id: PositiveID
    share: Percent
    state: Literal["active"] = "active"


class Row(StrictModel):
    offer_id: PositiveID
    name: str
    share: Percent
    state: Literal["active", "removed", "external_removed"] = "active"
    pinned: bool = False
    available: bool
    added: bool = False


class Difference(StrictModel):
    added: list[int] = []
    removed: list[int] = []
    changed: list[int] = []
    stream_changed: bool = False


class EditorError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def bindings(rows: list[Row]) -> list[Binding]:
    return [Binding(offer_id=r.offer_id, share=r.share) for r in rows if r.state == "active"]


def metadata(rows: list[Row]) -> list[tuple]:
    return [(r.offer_id, r.pinned, r.state, r.added) for r in rows]


def difference(base: list[Binding], remote: list[Binding]) -> Difference:
    old = {b.offer_id: b.share for b in base}
    new = {b.offer_id: b.share for b in remote}
    return Difference(added=sorted(new.keys() - old.keys()), removed=sorted(old.keys() - new.keys()),
                      changed=sorted(i for i in new.keys() & old.keys() if old[i] != new[i]))


def describe(rows: list[Row], catalog: dict) -> None:
    for row in rows:
        offer = catalog.get(row.offer_id)
        row.name = offer["name"] if offer else f"Оффер #{row.offer_id}"
        row.available = bool(offer and offer["state"] == "active")


def import_rows(saved: list[Row], remote: list[Binding], catalog: dict) -> list[Row]:
    by_id = {r.offer_id: r.model_copy() for r in saved}
    active = {b.offer_id for b in remote}
    for row in by_id.values():
        if row.state == "active" and row.offer_id not in active:
            row.state, row.share, row.pinned = "external_removed", 0, False
    for item in remote:
        row = by_id.get(item.offer_id)
        pinned = bool(row and row.state == "active" and row.share == item.share and row.pinned)
        by_id[item.offer_id] = Row(offer_id=item.offer_id, share=item.share, name="",
                                   available=False, pinned=pinned)
    rows = [by_id[k] for k in sorted(by_id)]
    describe(rows, catalog)
    return rows


def distribute(rows: list[Row]) -> None:
    active = [r for r in rows if r.state == "active"]
    if not active:
        return
    left = 100 - sum(r.share for r in active if r.pinned)
    free = sorted((r for r in active if not r.pinned), key=lambda r: r.offer_id)
    if left < len(free) or (not free and left != 0):
        raise EditorError("share_budget")
    if free:
        share, remainder = divmod(left, len(free))
        for index, row in enumerate(free):
            row.share = share + (index < remainder)


def apply_action(rows: list[Row], action: Action, offer_id: int, catalog: dict,
                 base_ids: set[int]) -> list[Row]:
    result = [r.model_copy() for r in rows]
    row = next((r for r in result if r.offer_id == offer_id), None)
    if action in {"add", "restore"}:
        if row and row.state == "active" and action == "add":
            return result
        if not catalog.get(offer_id) or catalog[offer_id]["state"] != "active":
            raise EditorError("offer_unavailable")
        if action == "restore" and (row is None or row.state == "active"):
            raise EditorError("invalid_action")
        if row is None:
            row = Row(offer_id=offer_id, share=0, available=True, name=catalog[offer_id]["name"])
            result.append(row)
        row.state, row.pinned, row.added = "active", False, offer_id not in base_ids
        describe([row], catalog)
    elif row is None or row.state != "active":
        raise EditorError("invalid_action")
    elif action == "remove":
        row.state, row.share, row.pinned, row.added = "removed", 0, False, False
    elif action == "pin":
        row.pinned = True
        return result
    elif not row.pinned:
        return result
    else:
        row.pinned = False
    distribute(result)
    return sorted(result, key=lambda r: r.offer_id)
