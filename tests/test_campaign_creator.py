import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.api.campaigns import get_connection
from app.core.config import Settings
from app.integrations.keitaro import KeitaroClient
from app.models import ManagedCampaign
from app.services import campaigns as service

MARKER = "synthetic-private-field"
ORDER = [
    "GET domains", "GET groups", "GET traffic_sources", "GET offers", "POST campaigns",
    "GET campaigns/21", "GET campaigns/21", "POST streams", "GET streams/31",
    "GET campaigns/21", "POST streams", "GET streams/32", "GET campaigns/21/streams",
]


class Tracker:
    def __init__(self):
        self.calls = []
        self.bodies = []
        self.campaign = {}
        self.streams = {}
        self.overrides = {}

    def __call__(self, request):
        path = request.url.path.removeprefix("/admin_api/v1/")
        step = len(self.calls)
        self.calls.append(f"{request.method} {path}")
        payload = json.loads(request.content) if request.content else None
        self.bodies.append(payload)
        if path == "domains":
            data = [{"id": 11}]
        elif path == "groups":
            data = [{"id": 12}]
        elif path == "traffic_sources":
            data = [{"id": 13}]
        elif path == "offers":
            data = [{"id": 51, "name": "Demo", "state": "active", "url": MARKER}]
        elif path == "campaigns" and request.method == "POST":
            self.campaign = dict(payload, id=21, group_id=12, uniqueness_method="ip", token=MARKER)
            data = self.campaign
        elif path == "campaigns/21":
            data = self.campaign
        elif path == "streams" and request.method == "POST":
            sid = 31 + len(self.streams)
            row = dict(filters=[], offers=[], landings=[], action_payload=None)
            row.update(payload, id=sid, position=sid - 30)
            self.streams[sid] = row
            data = row
        elif path.startswith("streams/"):
            data = self.streams[int(path.split("/")[1])]
        elif path == "campaigns/21/streams":
            data = list(reversed(self.streams.values()))
        else:
            pytest.fail(f"Unexpected mock route: {request.method} {path}")
        if step in self.overrides:
            override = self.overrides[step]
            if isinstance(override, Exception):
                raise override
            data = override(deepcopy(data)) if callable(override) else override
        return data if isinstance(data, httpx.Response) else httpx.Response(200, json=data)


@pytest.fixture
def setup(alembic_config, database_url):
    from app.main import create_app

    command.upgrade(alembic_config, "head")
    settings = Settings(database_url=database_url, keitaro_api_key=MARKER,
                        keitaro_base_url="https://tracker.test/admin_api/v1",
                        keitaro_domain_id=11, keitaro_group_id=12, keitaro_traffic_source_id=13)
    app = create_app(settings)
    tracker = Tracker()

    @contextmanager
    def opened():
        with KeitaroClient(settings, transport=httpx.MockTransport(tracker)) as client:
            yield client

    def provide():
        with opened() as client:
            yield lambda: client

    app.dependency_overrides[get_connection] = provide
    with TestClient(app) as api:
        yield api, tracker


@pytest.fixture
def body():
    return dict(operation_id=str(uuid4()), name="  Demo  ", geo="us", offer_id=51)


def post(api, body):
    return api.post("/api/campaigns", json=body)


def read(api, body):
    return api.get("/api/campaigns/operations/" + body["operation_id"]).json()


def test_confirmed_creation(setup, body):
    api, tracker = setup
    pool = api.app.state.session_factory.kw["bind"].pool
    def no_open_connection(data):
        assert pool.checkedout() == 0
        return data
    tracker.overrides = dict.fromkeys(range(len(ORDER)), no_open_connection)
    response = post(api, body)
    assert response.status_code == 201 and MARKER not in response.text
    result = response.json()
    assert (result["status"], result["stage"], result["geo"]) == ("succeeded", "complete", "US")
    assert [result[k] for k in ("remote_campaign_id", "regular_stream_id", "default_stream_id")] == [21, 31, 32]
    assert result["alias"].endswith(body["operation_id"].replace("-", ""))
    assert tracker.calls == ORDER
    assert tracker.bodies[4] == dict(name="test_shapovalov_Demo", alias=result["alias"],
        domain_id=11, group_id="12", traffic_source_id=13, type="position", state="active",
        cookies_ttl=24, cost_type="CPC", cost_currency="USD", cost_auto=True)
    common = dict(campaign_id=21, action_type="http", state="active", collect_clicks=True, filter_or=False)
    assert tracker.bodies[7] == dict(common, name="Flow 1", type="regular", schema="redirect",
        action_payload="https://www.google.com", filters=[dict(name="country", mode="accept", payload=["US"])])
    assert tracker.bodies[10] == dict(common, name="Flow 2", type="default", schema="landings",
        offers=[dict(offer_id=51, share=100, state="active")])
    assert read(api, body) == result
    assert post(api, dict(body, name=result["name"], geo="US")).json() == result
    assert post(api, dict(body, geo="FR")).status_code == 409
    assert tracker.calls == ORDER


@pytest.mark.parametrize("changes", [
    {"name": "  "}, {"name": "Demo\n"}, {"name": "test_other_demo"}, {"name": "x" * 120},
    {"geo": "ZZ"}, {"geo": "ß"}, {"offer_id": True}, {"offer_id": 1.5},
    {"offer_id": "51"}, {"offer_id": 0}, {"operation_id": "bad"}, {"url": MARKER},
])
def test_invalid_input(setup, body, changes):
    api, tracker = setup
    response = post(api, body | changes)
    assert response.status_code == 422 and MARKER not in response.text
    assert response.json()["code"] == "invalid_input"
    assert not tracker.calls


@pytest.mark.parametrize("step,reply,status,code", [
    (0, [], 503, "configuration"), (1, [{"id": 12}, {"id": 12}], 502, "protocol"),
    (2, [{"id": True}], 502, "protocol"), (3, [], 422, "offer_unavailable"),
    (3, [{"id": 51, "name": "Example", "state": "disabled"}], 422, "offer_unavailable"),
    (3, [{"id": 51, "name": "Example"}], 502, "protocol"),
])
def test_reject_before_write(setup, body, step, reply, status, code):
    api, tracker = setup
    tracker.overrides[step] = reply
    response = post(api, body)
    assert (response.status_code, response.json()["code"]) == (status, code)
    assert read(api, body)["status"] == "failed"
    assert all(call.startswith("GET ") for call in tracker.calls)


@pytest.mark.parametrize("step,reply,status,known,http_status", [
    (4, httpx.Response(403, text=MARKER), "failed", [None, None, None], 502),
    (4, httpx.ReadTimeout(MARKER), "unknown", [None, None, None], 504),
    (4, {"id": True}, "unknown", [None, None, None], 502),
    (4, httpx.Response(502), "unknown", [None, None, None], 502),
    (5, lambda r: dict(r, cost_auto=1), "partial", [21, None, None], 502),
    (5, httpx.ReadTimeout(MARKER), "partial", [21, None, None], 504),
    (6, httpx.ReadTimeout(MARKER), "partial", [21, None, None], 504),
    (7, httpx.Response(422), "partial", [21, None, None], 502),
    (7, httpx.ConnectError(MARKER), "unknown", [21, None, None], 502),
    (8, lambda r: dict(r, filters=[]), "partial", [21, 31, None], 502),
    (8, lambda r: dict(r, position=2), "partial", [21, 31, None], 502),
    (10, httpx.Response(429), "partial", [21, 31, None], 502),
    (10, {}, "unknown", [21, 31, None], 502),
    (10, {"id": 31}, "unknown", [21, 31, None], 502),
    (11, lambda r: dict(r, offers=[]), "partial", [21, 31, 32], 502),
    (12, lambda rows: rows[:1], "partial", [21, 31, 32], 502),
    (12, lambda rows: [dict(rows[0], position=1), rows[1]], "partial", [21, 31, 32], 502),
    (12, lambda rows: [dict(rows[0], filters=[{}]), rows[1]], "partial", [21, 31, 32], 502),
])
def test_partial_failures(setup, body, step, reply, status, known, http_status, caplog):
    api, tracker = setup
    tracker.overrides[step] = reply
    caplog.set_level("INFO")
    response = post(api, body)
    assert response.status_code == http_status
    assert MARKER not in response.text + caplog.text
    result = read(api, body)
    assert result == response.json()["operation"]
    assert result["status"] == status
    if step == 6:
        assert result["stage"] == "check_regular_campaign"
    assert result["outcome_unknown"] == (status == "unknown")
    assert [result[k] for k in ("remote_campaign_id", "regular_stream_id", "default_stream_id")] == known
    again = post(api, body)
    assert again.status_code == 200 and again.json() == result
    assert tracker.calls == ORDER[:step + 1]


def test_summary_list_requires_final_detail_reads(setup, body):
    api, tracker = setup
    tracker.overrides[12] = [{"id": 32}, {"id": 31}]
    tracker.overrides[14] = lambda row: dict(row, landings=[{"id": 1}])
    assert post(api, body).json()["operation"]["status"] == "partial"
    assert tracker.calls == ORDER + ["GET streams/31", "GET streams/32"]


def test_storage_failure_after_post(setup, body):
    api, tracker = setup
    engine = api.app.state.session_factory.kw["bind"]
    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE managed_campaigns SET remote_campaign_id"):
            raise OperationalError("synthetic", None, Exception(MARKER))
    event.listen(engine, "before_cursor_execute", fail)
    try:
        response = post(api, body)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert response.status_code == 503 and MARKER not in response.text
    assert response.json()["observed_ids"] == {"campaign_id": 21}
    result = read(api, body)
    assert (result["status"], result["stage"], result["outcome_unknown"]) == ("running", "post_campaign", True)
    assert result["remote_campaign_id"] is None and result["reconciliation_required"]
    assert post(api, body).status_code == 409
    assert tracker.calls == ORDER[:5]


def test_racing_reservations(setup, body, monkeypatch):
    api, tracker = setup
    barrier = Barrier(2)
    original = service.replay
    def simultaneous(factory, data):
        result = original(factory, data)
        if result is None:
            barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(service, "replay", simultaneous)
    with ThreadPoolExecutor(2) as workers:
        responses = [workers.submit(post, api, body) for _ in range(2)]
        codes = [future.result(timeout=10).status_code for future in responses]
    assert sorted(codes) in ([200, 201], [201, 409])
    assert tracker.calls == ORDER
    with api.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ManagedCampaign)) == 1


def test_restart_does_not_resume(setup, body, database_url):
    from app.main import create_app

    api, tracker = setup
    factory = api.app.state.session_factory
    data = service.CreateCampaign(**body)
    service.reserve(factory, data, "test_shapovalov_")
    service.checkpoint(factory, data.operation_id, "post_campaign", outcome_unknown=True)
    with TestClient(create_app(Settings(database_url=database_url, campaign_name_prefix="test_new_"))) as restarted:
        assert post(restarted, body).status_code == 409
        assert read(restarted, body)["stage"] == "post_campaign"
    assert not tracker.calls


def test_offers_search(setup):
    api, tracker = setup
    rows = [dict(id=i, name=f"Пример {i}", state="active", url=MARKER) for i in range(1, 26)]
    tracker.overrides = dict.fromkeys(range(3), rows)
    response = api.get("/api/offers", params={"q": "ПРИМЕР", "limit": 2})
    assert response.json() == [{"id": 1, "name": "Пример 1"}, {"id": 2, "name": "Пример 2"}]
    assert len(api.get("/api/offers").json()) == 20
    assert api.get("/api/offers?q=25").json() == [{"id": 25, "name": "Пример 25"}]
    tracker.overrides[3] = rows + [rows[0]]
    assert api.get("/api/offers?limit=1").status_code == 502
    assert api.get("/api/offers?limit=101").status_code == 422
    assert tracker.calls == ["GET offers"] * 4


def test_missing_credentials(alembic_config, client, body):
    command.upgrade(alembic_config, "head")
    assert client.get("/health").status_code == 200
    assert client.get("/api/offers").status_code == 503
    response = post(client, body)
    assert response.status_code == 503 and response.json()["code"] == "configuration"
    assert client.get(f"/api/campaigns/operations/{uuid4()}").status_code == 404


def test_failed_intent_commit_prevents_stream_post(setup, body, monkeypatch):
    api, tracker = setup
    real = service.checkpoint
    def fail_intent(factory, key, stage, **kwargs):
        if stage == "post_regular":
            raise service.CampaignError("storage", key)
        return real(factory, key, stage, **kwargs)
    monkeypatch.setattr(service, "checkpoint", fail_intent)
    response = post(api, body)
    assert response.status_code == 503
    assert read(api, body)["stage"] == "check_regular_campaign"
    assert tracker.calls == ORDER[:7]


def test_alias_differs_for_equal_names_and_distinct_uuids(setup, body):
    api, tracker = setup
    factory = api.app.state.session_factory
    first, _ = service.reserve(factory, service.CreateCampaign(**body), "test_shapovalov_")
    second, _ = service.reserve(factory, service.CreateCampaign(**(body | {"operation_id": uuid4()})),
                                "test_shapovalov_")
    assert first.alias != second.alias and first.name == second.name
    assert not tracker.calls
