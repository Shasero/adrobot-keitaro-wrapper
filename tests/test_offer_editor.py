import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event, local

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError

from app.api.campaigns import get_connection
from app.core.config import Settings
from app.db.session import create_db_engine
from app.integrations.keitaro import KeitaroClient
from app.main import create_app
from app.models import OfferDraft
from app.services.offer_shares import Row, distribute
from app.services.offer_store import Store

ROOT = "/api/campaigns/1/streams/2/editor"
PRIVATE = "synthetic-private-marker"


def binding(key, share):
    return dict(offer_id=key, share=share, state="active")


class Remote:
    def __init__(self):
        self.campaign = dict(id=1, name="test_shapovalov_manual", token=PRIVATE)
        self.stream = dict(id=2, campaign_id=1, type="regular", name="Offers", schema="landings",
                           state="active", position=1, action_type="http", action_payload=None,
                           filters=[], landings=[], collect_clicks=True, offers=[binding(10, 100)])
        self.catalog = [dict(id=i, name=f"Demo {i}", state="active", url=PRIVATE)
                        for i in (10, 20, 30, 40)]
        self.calls = []
        self.puts = []
        self.put_reply = None
        self.after = None
        self.before = None
        self.listing = None

    def __call__(self, request):
        assert getattr(self.connections, "count", 0) == 0
        path = request.url.path.removeprefix("/admin_api/v1/")
        assert not request.url.query
        self.calls.append((request.method, path))
        if path == "campaigns":
            data = [self.campaign, dict(id=22, name="other")]
        elif path == "campaigns/1":
            data = self.campaign
        elif path == "campaigns/1/streams":
            data = [self.stream] if self.listing is None else self.listing
        elif path == "offers":
            data = self.catalog
        elif path == "streams/2" and request.method == "GET":
            if self.before:
                self.before(self)
            data = self.stream
        elif path == "streams/2" and request.method == "PUT":
            payload = json.loads(request.content)
            self.puts.append(payload)
            if isinstance(self.put_reply, Exception):
                raise self.put_reply
            if self.put_reply is not None:
                return self.put_reply
            self.stream["offers"] = deepcopy(payload["offers"])
            if self.after:
                self.after(self)
            data = self.stream
        else:
            pytest.fail(f"Unexpected request {request.method} {path}")
        return httpx.Response(200, json=data)


@pytest.fixture
def setup(database_url, alembic_config):
    command.upgrade(alembic_config, "head")
    remote = Remote()
    settings = Settings(database_url=database_url, keitaro_api_key=PRIVATE,
                        keitaro_base_url="https://tracker.test/admin_api/v1")

    def start():
        app = create_app(settings)
        connections = local()
        remote.connections = connections
        engine = app.state.session_factory.kw["bind"]
        @event.listens_for(engine, "checkout")
        def checkout(*args):
            connections.count = getattr(connections, "count", 0) + 1
        @event.listens_for(engine, "checkin")
        def checkin(*args):
            connections.count -= 1

        def client():
            with KeitaroClient(settings, transport=httpx.MockTransport(remote)) as kt:
                yield lambda: kt

        app.dependency_overrides[get_connection] = client
        return TestClient(app)

    with start() as api:
        yield api, remote, start


def call(api, verb, current, **fields):
    return api.post(ROOT + "/" + verb, json=dict(expected_revision=current["revision"], **fields))


def action(api, current, name, key):
    response = call(api, "actions", current, action=name, offer_id=key)
    assert response.status_code == 200, response.text
    return response.json()


def draft(api):
    return action(api, api.get(ROOT).json(), "add", 20)


@pytest.mark.parametrize("count,expected", [(1,[100]), (2,[50,50]), (3,[34,33,33]),
                                           (6,[17,17,17,17,16,16])])
def test_stable_shares(count, expected):
    rows = [Row(offer_id=i+1, name="x", share=0, available=True) for i in reversed(range(count))]
    distribute(rows)
    assert [r.share for r in reversed(rows)] == expected


def test_manual_campaign_full_cycle(setup):
    api, remote, restart = setup
    assert api.get("/api/campaigns").json() == [dict(id=1, name=remote.campaign["name"])]
    assert api.get("/api/campaigns/1/streams").json() == [dict(id=2, name="Offers")]
    current = action(api, draft(api), "add", 30)
    for act, key, shares in [
        ("pin",10,[34,33,33]), ("add",40,[34,22,22,22]), ("remove",20,[34,0,33,33]),
        ("restore",20,[34,22,22,22]), ("remove",20,[34,0,33,33]),
    ]:
        current = action(api, current, act, key)
        assert [r["share"] for r in current["rows"]] == shares
    assert action(api, current, "add", 30) == current
    assert not remote.puts
    with restart() as other:
        assert other.get(ROOT).json() == current
        result = call(other, "push", current)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert not saved["dirty"] and not saved["metadata_changed"]
        assert saved["rows"][0]["pinned"] and saved["rows"][1]["state"] == "removed"
        assert remote.puts == [dict(offers=[binding(10,34),binding(30,33),binding(40,33)])]
        assert remote.calls[-1] == ("GET", "streams/2")
        assert call(other, "push", current).status_code == 409
        edit = action(other, saved, "restore", 20)
        assert call(other, "cancel", edit).json()["rows"] == saved["rows"]
    assert len(remote.puts) == 1


def test_metadata_budget_cancel_and_empty(setup):
    api, remote, _ = setup
    initial = api.get(ROOT).json()
    pin = action(api, initial, "pin", 10)
    assert not pin["dirty"] and pin["metadata_changed"]
    assert call(api,"actions",pin,action="add",offer_id=20).status_code == 422
    assert api.get(ROOT).json() == pin
    saved = call(api, "push", pin).json()
    assert not saved["metadata_changed"]
    removed = action(api,saved,"remove",10)
    assert not removed["rows"][0]["pinned"]
    assert call(api,"push",removed).json()["code"] == "empty_target"
    restored = action(api,api.get(ROOT).json(),"add",10)
    assert not restored["rows"][0]["pinned"]
    assert call(api,"cancel",restored).json()["rows"] == saved["rows"]
    assert not remote.puts


def test_external_remove_conflict_and_discard(setup):
    api, remote, _ = setup
    remote.stream["offers"] = [binding(10,75),binding(20,25)]
    state = api.get(ROOT).json()
    state = call(api,"push",action(api,state,"pin",10)).json()
    remote.stream["offers"] = [binding(20,11),binding(30,89)]
    changed = call(api,"refresh",state).json()
    assert changed["changes"] == dict(added=[30],removed=[10],changed=[20],stream_changed=False)
    assert [(r["state"],r["share"],r["pinned"]) for r in changed["rows"]] == [
        ("external_removed",0,False),("active",11,False),("active",89,False)]
    local = action(api,changed,"restore",10)
    remote.stream["offers"] = [binding(30,100)]
    result = call(api,"push",local)
    conflict = result.json()["editor"]
    assert result.status_code == 409 and conflict["rows"] == local["rows"]
    assert call(api,"refresh",conflict).status_code == 409
    reset = call(api,"cancel",api.get(ROOT).json()).json()
    assert call(api,"refresh",reset).json()["rows"][1]["state"] == "external_removed"
    assert not remote.puts


@pytest.mark.parametrize("field,value", [("schema","redirect"),("campaign_id",True),("id",3),
    ("offers",[binding(True,100)]),("offers",[binding(10,99.0)]),
    ("offers",[binding(10,50),binding(10,50)]),("offers",[binding(10,99)]),
    ("offers",[dict(offer_id=10,share=100,state="disabled")])])
def test_unsupported(setup,field,value):
    api, remote, _ = setup
    remote.stream[field] = value
    assert api.get(ROOT).status_code in (403,422,502)
    assert not remote.puts


def test_catalog_and_guards(setup):
    api, remote, _ = setup
    remote.catalog = [dict(id=20,state="disabled",name="Disabled")]
    current = api.get(ROOT).json()
    assert current["rows"][0]["name"] == "Оффер #10"
    for key in (20,30,True,1.2):
        assert call(api,"actions",current,action="add",offer_id=key).status_code == 422
    remote.campaign["name"] = "foreign"
    assert api.get(ROOT).status_code == 403
    remote.campaign["name"] = "test_shapovalov_ok"
    remote.listing = [dict(id=2,campaign_id=9)]
    assert api.get(ROOT).status_code == 403
    assert not remote.puts


@pytest.mark.parametrize("reply,code,status", [
    (httpx.Response(400,text=PRIVATE),"upstream","ready"),
    (httpx.Response(500,text=PRIVATE),"upstream","unknown"),
    (httpx.ReadTimeout(PRIVATE),"timeout","unknown"),
    (httpx.ConnectError(PRIVATE),"upstream","unknown"),
    (httpx.Response(200,text=PRIVATE),"protocol","unknown"),
    (httpx.Response(200,json={}),"protocol","unknown"),
])
def test_publish_failure(setup,reply,code,status,caplog):
    api, remote, restart = setup
    current = draft(api)
    remote.put_reply = reply
    failed = call(api,"push",current)
    assert failed.json()["code"] == code
    state = failed.json()["editor"]
    assert state["status"] == status and state["dirty"]
    assert PRIVATE not in failed.text + caplog.text
    with restart() as other:
        if status == "unknown":
            assert call(other,"cancel",state).status_code == 409
            assert call(other,"push",state).status_code == 409
            checked = call(other,"refresh",state).json()
            assert checked["error_code"] == "base_confirmed" and checked["dirty"]
    assert len(remote.puts) == 1


@pytest.mark.parametrize("field,value", [("position",2),("state","disabled"),("filters",[{}]),
    ("action_payload","changed"),("landings",[{}]),("offers",[binding(10,100)]),
    ("collect_clicks",False)])
def test_verify_and_reconcile(setup,field,value):
    api, remote, _ = setup
    state = draft(api)
    original = deepcopy(remote.stream)
    remote.after = lambda r: r.stream.update({field:value})
    failed = call(api,"push",state).json()["editor"]
    assert failed["status"] == "verification_failed"
    if field != "offers":
        locked = call(api,"refresh",failed)
        assert locked.status_code == 409
        failed = locked.json()["editor"]
        assert call(api,"cancel",failed).status_code == 409
    remote.stream = dict(original,offers=remote.puts[0]["offers"][::-1],updated_at="new")
    fixed = call(api,"refresh",failed).json()
    assert fixed["status"] == "ready" and not fixed["dirty"] and len(remote.puts) == 1


def test_verification_timeout_and_storage_failure(setup,monkeypatch):
    api, remote, restart = setup
    state = draft(api)
    def fail(r):
        raise httpx.ReadTimeout(PRIVATE)
    remote.after = lambda r: setattr(r,"before",fail)
    response = call(api,"push",state)
    assert response.status_code == 504 and response.json()["editor"]["status"] == "verification_failed"
    remote.before = remote.after = None
    done = call(api,"refresh",response.json()["editor"]).json()
    state = action(api,done,"remove",10)
    update = Store.update
    def broken(store, rev, doc):
        if doc.stage == "verify":
            raise OperationalError("synthetic",{},None)
        return update(store,rev,doc)
    monkeypatch.setattr(Store,"update",broken)
    assert call(api,"push",state).status_code == 503
    with restart() as other:
        pending = other.get(ROOT).json()
        assert pending["status"] == "publishing" and pending["outcome_unknown"]
        assert call(other,"refresh",pending).json()["dirty"] is False
    assert len(remote.puts) == 2


def test_concurrency_and_intent(setup):
    api, remote, _ = setup
    state = draft(api)
    entered, release = Event(), Event()
    def wait_after(r):
        entered.set()
        assert release.wait(5)
    remote.after = wait_after
    with ThreadPoolExecutor(2) as pool:
        task = pool.submit(call,api,"push",state)
        assert entered.wait(5)
        try:
            with api.app.state.session_factory() as db:
                row = db.get(OfferDraft,(1,2))
                assert row.document["target"] == [binding(10,50),binding(20,50)]
                assert row.document["stage"] == "put"
                live = dict(revision=row.revision)
            for verb in ("refresh","push","cancel"):
                assert call(api,verb,live).status_code == 409
            assert call(api,"actions",live,action="remove",offer_id=10).status_code == 409
        finally:
            release.set()
        assert task.result().status_code == 200
    assert len(remote.puts) == 1


def test_concurrent_local_updates(setup,monkeypatch):
    api,remote,_ = setup
    state = api.get(ROOT).json()
    gate = Barrier(2)
    update = Store.update
    def race(store,rev,doc):
        gate.wait(5)
        return update(store,rev,doc)
    monkeypatch.setattr(Store,"update",race)
    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(call,api,"actions",state,action="add",offer_id=key) for key in (20,30)]
        assert sorted(t.result().status_code for t in tasks) == [200,409]
    assert len(api.get(ROOT).json()["rows"]) == 2 and not remote.puts


def test_final_guard_and_unpin(setup):
    api,remote,_ = setup
    remote.stream["offers"] = [binding(20,21),binding(10,79)]
    current = api.get(ROOT).json()
    pinned = action(api,current,"pin",10)
    assert [r["share"] for r in pinned["rows"]] == [79,21]
    state = action(api,pinned,"unpin",10)
    assert [r["share"] for r in state["rows"]] == [50,50]
    reads = []
    def drift(r):
        reads.append(1)
        if len(reads) == 2:
            r.stream["offers"] = [binding(20,100)]
    remote.before = drift
    assert call(api,"push",state).json()["code"] == "conflict"
    assert not remote.puts


def test_migration_preserves_operations(alembic_config,database_url):
    command.upgrade(alembic_config,"0002")
    engine = create_db_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO managed_campaigns(id,name) VALUES (1,'Legacy')"))
            conn.execute(text("INSERT INTO campaign_operations(operation_id,managed_id,alias,"
                              "name_prefix,geo,offer_id,stage) VALUES ('old',1,'alias','prefix','US',1,'ok')"))
        for revision in ("head","0002","head"):
            if revision == "head":
                command.upgrade(alembic_config,revision)
                command.check(alembic_config)
            else:
                command.downgrade(alembic_config,revision)
            assert inspect(engine).has_table("offer_drafts") == (revision == "head")
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT name FROM managed_campaigns")) == "Legacy"
                assert conn.scalar(text("SELECT operation_id FROM campaign_operations")) == "old"
    finally:
        engine.dispose()


def test_clean_refresh_keeps_unequal_pins_and_ignores_binding_metadata(setup):
    api, remote, _ = setup
    remote.stream["offers"] = [binding(10,70),binding(20,30)]
    saved = call(api,"push",action(api,api.get(ROOT).json(),"pin",20)).json()
    remote.stream["offers"] = [dict(binding(20,30),id=701,updated_at="later"),binding(10,70)]
    unchanged = call(api,"refresh",saved).json()
    assert unchanged["rows"] == saved["rows"]
    remote.stream["offers"] = [binding(10,60),binding(20,40)]
    changed = call(api,"refresh",unchanged).json()
    assert changed["changes"]["changed"] == [10,20]
    assert [r["share"] for r in changed["rows"]] == [60,40]
    assert not any(r["pinned"] for r in changed["rows"]) and not remote.puts


def test_drift_of_catalog_blocks_push(setup):
    api, remote, _ = setup
    state = draft(api)
    remote.catalog = [r for r in remote.catalog if r["id"] != 20]
    result = call(api,"push",state)
    assert result.status_code == 422 and result.json()["editor"]["dirty"]
    assert not remote.puts


def test_unknown_put_applied_then_explicit_refresh(setup):
    api, remote, _ = setup
    state = draft(api)
    def lost_response(r):
        raise httpx.ReadTimeout(PRIVATE)
    remote.after = lost_response
    result = call(api,"push",state).json()["editor"]
    assert result["status"] == "unknown"
    remote.stream["offers"][0]["state"] = "disabled"
    locked = call(api,"refresh",result)
    assert locked.status_code == 409
    remote.stream["offers"][0]["state"] = "active"
    done = call(api,"refresh",locked.json()["editor"]).json()
    assert done["status"] == "ready" and not done["dirty"]
    assert len(remote.puts) == 1
