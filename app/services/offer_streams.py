"""Translate private Keitaro records into validated offer snapshots."""
import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from app.integrations.keitaro import KeitaroClient, Record
from app.services.campaigns import catalogue, valid_id
from app.services.offer_shares import Binding, EditorError, PositiveID, StrictModel


class Choice(StrictModel):
    id: PositiveID
    name: str


def choice(row: Record) -> Choice:
    if not valid_id(row.get("id")) or not isinstance(row.get("name"), str) or not row["name"].strip():
        raise EditorError("protocol")
    return Choice(id=row["id"], name=row["name"])


def campaigns(client: KeitaroClient, prefix: str) -> list[Choice]:
    items = [choice(row) for row in catalogue(client.list_campaigns()).values()]
    return [i for i in items if i.name.startswith(prefix) and i.name[len(prefix):].strip()]


def streams(client: KeitaroClient, campaign_id: int) -> list[Choice]:
    campaign = client.checked_campaign(campaign_id)
    listing = catalogue(client.list_campaign_streams(campaign_id))
    choices = []
    for key, row in listing.items():
        if "campaign_id" in row and (not valid_id(row["campaign_id"]) or row["campaign_id"] != campaign_id):
            raise EditorError("forbidden")
        if "campaign_name" in row and row["campaign_name"] != campaign["name"]:
            raise EditorError("forbidden")
        if not {"name", "type", "schema", "campaign_id"} <= row.keys():
            row = client.checked_stream(campaign_id, key)
        item = choice(row)
        if not valid_id(row.get("campaign_id")) or row["campaign_id"] != campaign_id:
            raise EditorError("forbidden")
        if not all(isinstance(row.get(k), str) and row[k] for k in ("type", "schema")):
            raise EditorError("protocol")
        if row["type"] in ("regular", "default") and row["schema"] == "landings":
            choices.append(item)
    return choices


@dataclass
class Snapshot:
    offers: list[Binding]
    fingerprint: str


def fingerprint(stream: Record) -> str:
    # Keep opaque configuration out of SQLite. Preserve array ordering when order
    # can affect routing. Keitaro timestamps do not describe routing behavior.
    def without_timestamps(value):
        if isinstance(value, list):
            return [without_timestamps(v) for v in value]
        if isinstance(value, dict):
            return {k: without_timestamps(v) for k, v in value.items()
                    if k not in ("created_at", "updated_at")}
        return value

    settings = without_timestamps({k: v for k, v in stream.items() if k != "offers"})
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def snapshot(stream: Record, campaign_id: int, stream_id: int) -> Snapshot:
    for field, key in (("id", stream_id), ("campaign_id", campaign_id)):
        if not valid_id(stream.get(field)) or stream[field] != key:
            raise EditorError("forbidden")
    choice(stream)
    if stream.get("schema") != "landings" or stream.get("type") not in ("regular", "default"):
        raise EditorError("unsupported_stream")
    if (stream.get("state") not in ("active", "disabled") or not valid_id(stream.get("position"))
            or not isinstance(stream.get("action_type"), str) or not stream["action_type"]
            or "action_payload" not in stream
            or not all(isinstance(stream.get(k), list) for k in ("filters", "landings", "offers"))):
        raise EditorError("protocol")
    active = {}
    for row in stream["offers"]:
        if not isinstance(row, dict):
            raise EditorError("protocol")
        if "stream_id" in row and (not valid_id(row["stream_id"]) or row["stream_id"] != stream_id):
            raise EditorError("protocol")
        try:
            item = Binding.model_validate({k: row[k] for k in ("offer_id", "share", "state")})
        except (KeyError, ValidationError):
            raise EditorError("unsupported_bindings") from None
        if item.offer_id in active or not item.share:
            raise EditorError("unsupported_bindings")
        active[item.offer_id] = item
    if active and sum(b.share for b in active.values()) != 100:
        raise EditorError("unsupported_bindings")
    return Snapshot([active[k] for k in sorted(active)], fingerprint(stream))
