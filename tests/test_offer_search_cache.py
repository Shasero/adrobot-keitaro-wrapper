from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.campaigns import get_connection
from app.core.config import Settings
from app.integrations.keitaro import KeitaroClient
from app.main import create_app
from app.services import offer_search


class CatalogBackend:
    def __init__(self, hostname, name):
        self.hostname = hostname
        self.rows = [{"id": 18001, "name": name, "state": "active", "url": "synthetic-private"}]
        self.calls = []
        self.opens = 0
        self.response = lambda: httpx.Response(200, json=self.rows)

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.url.host == self.hostname
        assert self.calls[-1] == ("GET", "/admin_api/v1/offers")
        return self.response()

    def attach(self, app, settings):
        def provide():
            with ExitStack() as stack:
                def connect():
                    self.opens += 1
                    return stack.enter_context(KeitaroClient(settings, transport=httpx.MockTransport(self)))

                yield connect

        app.dependency_overrides[get_connection] = provide


def make_search_app(database_url, hostname, name):
    settings = Settings(database_url=database_url, keitaro_api_key="synthetic-fixture",
                        keitaro_base_url=f"https://{hostname}/admin_api/v1")
    app = create_app(settings)
    backend = CatalogBackend(hostname, name)
    backend.attach(app, settings)
    return app, backend


@pytest.fixture
def catalog(database_url):
    app, backend = make_search_app(database_url, "catalog.test", "First")
    with TestClient(app) as api:
        yield api, backend


def test_catalog_fills_on_first_search_and_keeps_only_search_fields(catalog):
    api, backend = catalog
    assert api.get("/health").status_code == 200
    assert backend.calls == []
    assert api.get("/api/offers?limit=101").status_code == 422
    assert backend.calls == []

    assert api.get("/api/offers?q=18001").json() == [{"id": 18001, "name": "First"}]
    assert api.get("/api/offers?q=FIRST").json() == [{"id": 18001, "name": "First"}]
    assert len(backend.calls) == backend.opens == 1
    assert api.app.state.offer_search_cache._catalog == (
        offer_search.CatalogOffer(id=18001, name="First", state="active"),
    )


def test_empty_catalog_is_a_valid_cache_hit(catalog):
    api, backend = catalog
    backend.rows = []
    assert api.get("/api/offers").json() == []
    assert api.get("/api/offers?q=18001").json() == []
    assert len(backend.calls) == 1


def test_catalog_expires_from_successful_load(catalog):
    api, backend = catalog
    now = 0
    api.app.state.offer_search_cache = offer_search.OfferSearchCache(
        api.app.state.settings.offer_cache_ttl_seconds, clock=lambda: now,
    )

    def cold_response():
        nonlocal now
        now += 30
        return httpx.Response(200, json=backend.rows)

    backend.response = cold_response
    assert api.get("/api/offers").json()[0]["name"] == "First"
    assert now == 30

    backend.rows[0]["name"] = "Updated"
    now = 329.999
    assert api.get("/api/offers").json()[0]["name"] == "First"
    assert len(backend.calls) == 1

    now = 330
    assert api.get("/api/offers").json()[0]["name"] == "Updated"
    assert len(backend.calls) == 2


@pytest.mark.parametrize("upstream_status", [200, 503])
def test_parallel_requests_share_one_refresh(catalog, monkeypatch, upstream_status):
    api, backend = catalog
    count = 4
    entered = Barrier(count, timeout=10)

    class ObservedFuture(Future):
        def result(self, timeout=None):
            entered.wait()
            return super().result(timeout)

    monkeypatch.setattr(offer_search, "Future", ObservedFuture)

    def blocked_response():
        entered.wait()
        return httpx.Response(upstream_status, json=backend.rows)

    backend.response = blocked_response
    with ThreadPoolExecutor(max_workers=count) as executor:
        requests = [executor.submit(api.get, "/api/offers") for _ in range(count)]
        responses = [request.result(timeout=15) for request in requests]

    assert len(backend.calls) == 1
    if upstream_status == 200:
        assert all(r.status_code == 200 and r.json() == [{"id": 18001, "name": "First"}] for r in responses)
    else:
        assert all(r.status_code == 502 and r.json()["code"] == "upstream" for r in responses)
        backend.response = lambda: httpx.Response(200, json=backend.rows)
        assert api.get("/api/offers").status_code == 200
        assert len(backend.calls) == 2


@pytest.mark.parametrize("bad_result,expected", [
    ("upstream", (502, "upstream")),
    ("timeout", (504, "timeout")),
    ("duplicate", (502, "protocol")),
    ("missing_state", (502, "protocol")),
])
def test_failed_refresh_returns_error_and_next_search_can_recover(catalog, bad_result, expected):
    api, backend = catalog
    now = 0
    api.app.state.offer_search_cache = offer_search.OfferSearchCache(300, clock=lambda: now)
    assert api.get("/api/offers").json()[0]["name"] == "First"
    now = 300

    def failed_response():
        if bad_result == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if bad_result == "upstream":
            return httpx.Response(503)
        if bad_result == "duplicate":
            return httpx.Response(200, json=backend.rows * 2)
        return httpx.Response(200, json=[{"id": 18001, "name": "Invalid"}])

    backend.response = failed_response
    failed = api.get("/api/offers")
    assert (failed.status_code, failed.json()["code"]) == expected
    assert "First" not in failed.text

    backend.rows[0]["name"] = "Recovered"
    backend.response = lambda: httpx.Response(200, json=backend.rows)
    assert api.get("/api/offers").json() == [{"id": 18001, "name": "Recovered"}]
    assert len(backend.calls) == 3


def test_search_caches_are_isolated_between_apps(database_url):
    first, first_backend = make_search_app(database_url, "first.test", "First")
    second, second_backend = make_search_app(database_url, "second.test", "Second")
    with TestClient(first) as one, TestClient(second) as two:
        assert one.get("/api/offers").json()[0]["name"] == "First"
        assert two.get("/api/offers").json()[0]["name"] == "Second"
        assert one.get("/api/offers").json()[0]["name"] == "First"
        assert two.get("/api/offers").json()[0]["name"] == "Second"
    assert len(first_backend.calls) == len(second_backend.calls) == 1
