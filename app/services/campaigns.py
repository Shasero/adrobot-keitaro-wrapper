import re
import unicodedata
from collections.abc import Callable
from functools import partial
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.integrations.keitaro import KeitaroClient, KeitaroError, Record
from app.models import CampaignOperation, ManagedCampaign

MAX_ID = 2**63 - 1
# ISO 3166-1 alpha-2, from the Debian iso-codes list.
COUNTRIES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM
BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX
CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG
GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR
IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV
LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE
NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF
TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF
WS YE YT ZA ZM ZW
""".split())


class CreateCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    geo: str
    offer_id: int = Field(strict=True, gt=0, le=MAX_ID)
    operation_id: UUID

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Invalid name")
        value = value.strip()
        if not value or len(value) > 120:
            raise ValueError("Invalid name length")
        return value

    @field_validator("geo")
    @classmethod
    def country(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch("[a-zA-Z]{2}", value) or value.upper() not in COUNTRIES:
            raise ValueError("Invalid country")
        return value.upper()


class Operation(BaseModel):
    operation_id: UUID
    name: str
    geo: str
    offer_id: int
    alias: str
    status: str
    stage: str
    remote_campaign_id: int | None
    regular_stream_id: int | None
    default_stream_id: int | None
    error_code: str | None
    outcome_unknown: bool
    reconciliation_required: bool
    advice: str | None


MESSAGES = {
    "invalid_input": (422, "Проверьте параметры запроса."),
    "configuration": (503, "Проверьте настройки интеграции и выбранные справочники."),
    "offer_unavailable": (422, "Выбранный оффер отсутствует или выключен."),
    "operation_conflict": (409, "Этот UUID уже связан с другими параметрами."),
    "running": (409, "Операция уже начата. Проверьте её состояние."),
    "not_found": (404, "Операция не найдена."),
    "protocol": (502, "Keitaro вернул некорректные данные."),
    "verification": (502, "Прочитанные настройки не совпали с ожидаемыми."),
    "upstream": (502, "Не удалось выполнить запрос к Keitaro."),
    "timeout": (504, "Истекло время ожидания ответа Keitaro."),
    "storage": (503, "Ошибка локальной БД. Проверьте сохранённую стадию и сверьте объекты вручную."),
}


class CampaignError(Exception):
    def __init__(self, code: str, operation_id: UUID | None = None,
                 operation: Operation | None = None):
        super().__init__(code)
        self.code = code
        self.operation_id = operation_id
        self.operation = operation
        self.observed_ids: dict[str, int] | None = None


def full_name(value: str, prefix: str) -> str:
    if not re.fullmatch(r"test_[A-Za-z]+(?:-[A-Za-z]+)*_", prefix):
        raise CampaignError("configuration")
    suffix = value.removeprefix(prefix)
    if not suffix.strip() or suffix.casefold().startswith("test_"):
        raise CampaignError("invalid_input")
    name = prefix + suffix
    if len(name) > 120:
        raise CampaignError("invalid_input")
    return name


def valid_id(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_ID


def catalogue(rows: list[Record]) -> dict[int, Record]:
    result = {}
    for row in rows:
        key = row.get("id")
        if not valid_id(key) or key in result:
            raise CampaignError("protocol")
        result[key] = row
    return result


def offers_by_id(client: KeitaroClient) -> dict[int, Record]:
    offers = catalogue(client.list_offers())
    for row in offers.values():
        for field in ("name", "state"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise CampaignError("protocol")
    return offers


def snapshot(op: CampaignOperation) -> Operation:
    campaign = op.campaign
    return Operation(
        operation_id=op.operation_id, name=campaign.name, geo=op.geo, offer_id=op.offer_id,
        alias=op.alias, status=campaign.status, stage=op.stage,
        remote_campaign_id=campaign.remote_campaign_id, regular_stream_id=op.regular_stream_id,
        default_stream_id=op.default_stream_id, error_code=op.error_code,
        outcome_unknown=op.outcome_unknown,
        reconciliation_required=campaign.status in {"running", "unknown", "partial"},
        advice=("Сверьте объекты по alias и известным ID до новой попытки."
                if campaign.status in {"running", "unknown", "partial"} else None),
    )


def read_operation(factory: sessionmaker, key: UUID) -> Operation:
    try:
        with factory() as db:
            op = db.get(CampaignOperation, str(key))
            if op is None:
                raise CampaignError("not_found", key)
            return snapshot(op)
    except SQLAlchemyError:
        raise CampaignError("storage", key) from None


def replay(factory: sessionmaker, data: CreateCampaign) -> Operation | None:
    with factory() as db:
        op = db.get(CampaignOperation, str(data.operation_id))
        if op is None:
            return None
        result = snapshot(op)
        try:
            name = full_name(data.name, op.name_prefix)
        except CampaignError:
            raise CampaignError("operation_conflict", data.operation_id) from None
        if (result.name, result.geo, result.offer_id) != (name, data.geo, data.offer_id):
            raise CampaignError("operation_conflict", data.operation_id)
        if result.status == "running":
            raise CampaignError("running", data.operation_id, result)
        return result


def reserve(factory: sessionmaker, data: CreateCampaign, prefix: str) -> tuple[Operation, bool]:
    try:
        existing = replay(factory, data)
        if existing is not None:
            return existing, False
        try:
            name = full_name(data.name, prefix)
        except CampaignError as exc:
            raise CampaignError(exc.code, data.operation_id) from None
        slug = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        op = CampaignOperation(
            operation_id=str(data.operation_id), geo=data.geo, offer_id=data.offer_id,
            alias=f"{slug}-{data.operation_id.hex}", name_prefix=prefix, stage="reserved",
            campaign=ManagedCampaign(name=name, status="running"),
        )
        try:
            with factory.begin() as db:
                db.add(op)
                db.flush()
                result = snapshot(op)
            return result, True
        except IntegrityError:
            # Another request may have committed the same UUID after our read.
            existing = replay(factory, data)
            if existing is not None:
                return existing, False
            raise
    except SQLAlchemyError:
        raise CampaignError("storage", data.operation_id) from None


def checkpoint(factory: sessionmaker, key: UUID, stage: str, *, status: str = "running",
               outcome_unknown: bool = False, error_code: str | None = None,
               campaign_id: int | None = None, regular_id: int | None = None,
               default_id: int | None = None) -> Operation:
    try:
        with factory.begin() as db:
            op = db.get(CampaignOperation, str(key))
            if op is None:
                raise CampaignError("storage", key)
            op.stage, op.error_code, op.outcome_unknown = stage, error_code, outcome_unknown
            op.campaign.status = status
            if campaign_id is not None:
                op.campaign.remote_campaign_id = campaign_id
            if regular_id is not None:
                op.regular_stream_id = regular_id
            if default_id is not None:
                op.default_stream_id = default_id
            result = snapshot(op)
        return result
    except SQLAlchemyError:
        raise CampaignError("storage", key) from None


def verify(actual: object, expected: object) -> None:
    """Ignore extra record fields, while comparing lists and scalar types strictly."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            if key not in actual:
                raise CampaignError("verification")
            verify(actual[key], value)
    elif isinstance(expected, list) and isinstance(actual, list) and len(actual) == len(expected):
        for item, wanted in zip(actual, expected):
            verify(item, wanted)
    elif type(actual) is not type(expected) or actual != expected:
        raise CampaignError("verification")


def created_id(response: Record) -> int:
    value = response.get("id")
    if not valid_id(value):
        raise KeitaroError("protocol", outcome_unknown=True)
    return value


def upstream_code(exc: KeitaroError) -> str:
    return exc.category if exc.category in {"configuration", "protocol", "timeout"} else "upstream"


def create(factory: sessionmaker, connect: Callable[[], KeitaroClient],
           settings: Settings, data: CreateCampaign) -> tuple[Operation, bool]:
    key = data.operation_id
    current, owner = reserve(factory, data, settings.campaign_name_prefix)
    name = current.name
    if not owner:
        return current, False

    def mark(stage: str, **changes) -> Operation:
        nonlocal current
        try:
            current = checkpoint(factory, key, stage, **changes)
        except CampaignError as exc:
            exc.observed_ids = {k: changes[k] for k in ("campaign_id", "regular_id", "default_id")
                                if k in changes} or None
            raise
        return current

    try:
        domain, group, source = (settings.keitaro_domain_id, settings.keitaro_group_id,
                                 settings.keitaro_traffic_source_id)
        if not all(valid_id(value) for value in (domain, group, source)):
            raise CampaignError("configuration")
        client = connect()
        for stage, lookup, wanted in [
            ("domains", client.list_domains, domain),
            ("groups", client.list_campaign_groups, group),
            ("sources", client.list_traffic_sources, source),
        ]:
            mark(stage)
            if wanted not in catalogue(lookup()):
                raise CampaignError("configuration")
        mark("offers")
        offer = offers_by_id(client).get(data.offer_id)
        if not offer or offer["state"] != "active":
            raise CampaignError("offer_unavailable")

        payload = dict(name=name, alias=current.alias, type="position", state="active",
                       domain_id=domain, group_id=str(group), traffic_source_id=source,
                       cookies_ttl=24, cost_type="CPC", cost_currency="USD", cost_auto=True)
        mark("post_campaign", outcome_unknown=True)
        campaign_id = created_id(client.create_campaign(payload))
        mark("verify_campaign", campaign_id=campaign_id)
        saved = client.get_campaign(campaign_id)
        expected = dict(payload, id=campaign_id, uniqueness_method="ip")
        if type(saved.get("group_id")) is int:
            expected["group_id"] = group
        verify(saved, expected)

        common = dict(campaign_id=campaign_id, action_type="http", state="active",
                      collect_clicks=True, filter_or=False)
        regular = dict(common, name="Flow 1", type="regular", schema="redirect",
                       action_payload="https://www.google.com",
                       filters=[dict(name="country", mode="accept", payload=[data.geo])])
        default = dict(common, name="Flow 2", type="default", schema="landings",
                       offers=[dict(offer_id=data.offer_id, share=100, state="active")])
        streams = {}
        for label, body, position in [("regular", regular, 1), ("default", default, 2)]:
            mark(f"check_{label}_campaign")
            intent = partial(mark, f"post_{label}", outcome_unknown=True)
            stream_id = created_id(client.create_stream(body, before_post=intent))
            if stream_id in streams:
                raise KeitaroError("protocol", outcome_unknown=True)
            mark(f"verify_{label}", **{f"{label}_id": stream_id})
            wanted = dict(filters=[], offers=[], landings=[], action_payload=None)
            wanted.update(body, id=stream_id, position=position)
            verify(client.get_stream(stream_id), wanted)
            streams[stream_id] = wanted

        mark("verify_streams")
        listing = catalogue(client.list_campaign_streams(campaign_id))
        if listing.keys() != streams.keys():
            raise CampaignError("verification")
        for stream_id, wanted in streams.items():
            row = listing[stream_id]
            verify(row, {field: wanted[field] for field in wanted.keys() & row.keys()})
            if not wanted.keys() <= row.keys():
                mark(f"verify_final_position_{wanted['position']}")
                verify(client.get_stream(stream_id), wanted)
        return mark("complete", status="succeeded"), True
    except (KeitaroError, CampaignError) as exc:
        if isinstance(exc, CampaignError) and exc.code == "storage":
            raise
        unknown = isinstance(exc, KeitaroError) and exc.outcome_unknown
        status = "unknown" if unknown else ("partial" if current.remote_campaign_id else "failed")
        code = upstream_code(exc) if isinstance(exc, KeitaroError) else exc.code
        saved = mark(current.stage, status=status, outcome_unknown=unknown, error_code=code)
        raise CampaignError(code, key, saved) from None
