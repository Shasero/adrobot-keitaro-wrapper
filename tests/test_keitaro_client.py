import json
import traceback

import httpx
import pytest

from app.core.config import Settings
from app.integrations.keitaro import KeitaroClient, KeitaroError

BASE = "https://tracker.test/admin_api/v1"
MARKER = "synthetic-private-marker"
CAMPAIGN = {"id": 11, "name": "test_example_campaign"}
STREAM = {"id": 22, "campaign_id": 11}


def settings(**overrides):
    values = {
        "keitaro_base_url": BASE,
        "keitaro_api_key": MARKER,
        "keitaro_timeout_seconds": 8,
        "campaign_name_prefix": "test_example_",
    }
    return Settings(_env_file=None, **(values | overrides))


def exchange(*responses, **overrides):
    requests = []
    replies = iter(responses)

    def handler(request):
        requests.append(request)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    client = KeitaroClient(settings(**overrides), transport=httpx.MockTransport(handler))
    return client, requests


@pytest.mark.parametrize(
    ("method", "args", "resource", "answer"),
    [
        ("list_domains", (), "domains", []),
        ("list_campaign_groups", (), "groups?type=campaigns", [{"id": 1}]),
        ("list_traffic_sources", (), "traffic_sources", []),
        ("list_offers", (), "offers", [{"id": 1}, {"id": 2}]),
        ("get_offer", (1,), "offers/1", {"id": 1}),
        ("list_campaigns", (), "campaigns", []),
        ("get_campaign", (11,), "campaigns/11", CAMPAIGN),
        ("list_campaign_streams", (11,), "campaigns/11/streams", [STREAM]),
        ("get_stream", (22,), "streams/22", STREAM),
    ],
)
@pytest.mark.parametrize("suffix", ["", "/"])
def test_read_contract(method, args, resource, answer, suffix):
    client, calls = exchange(answer, keitaro_base_url=BASE + suffix)
    with client:
        assert getattr(client, method)(*args) == answer
    request, = calls
    assert (request.method, str(request.url)) == ("GET", f"{BASE}/{resource}")
    assert request.headers["Api-Key"] == MARKER
    assert request.headers["Accept"] == "application/json"
    assert set(request.extensions["timeout"].values()) == {8}
    assert not request.content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("keitaro_api_key", None), ("keitaro_api_key", "  "),
        ("keitaro_api_key", MARKER + "\ninvalid"),
        ("keitaro_base_url", "http://tracker.test/admin_api/v1"),
        ("keitaro_base_url", "/admin_api/v1"),
        ("keitaro_base_url", "https://example.invalid/admin_api/v1"),
        ("keitaro_base_url", f"https://{MARKER}@tracker.test/admin_api/v1"),
        ("keitaro_base_url", BASE + "?key=" + MARKER),
        ("keitaro_base_url", BASE + "#" + MARKER),
        ("keitaro_base_url", BASE + "/extra"),
        ("keitaro_base_url", "https://tracker.test:99999/admin_api/v1"),
        ("campaign_name_prefix", ""), ("campaign_name_prefix", " "),
        ("campaign_name_prefix", "test_"), ("campaign_name_prefix", "test_*_"),
        ("campaign_name_prefix", "test_a b_"),
        ("keitaro_timeout_seconds", 0), ("keitaro_timeout_seconds", float("inf")),
    ],
)
def test_unready_configuration(field, value):
    config = settings()
    if field == "keitaro_timeout_seconds":
        # Exercise the client's check independently of Settings validation.
        config = config.model_copy(update={field: value})
    else:
        config = settings(**{field: value})
    with pytest.raises(KeitaroError) as caught:
        KeitaroClient(config, transport=httpx.MockTransport(lambda _: pytest.fail()))
    assert caught.value.category == "configuration"
    assert MARKER not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1", "https://other.test/"])
def test_invalid_ids_never_reach_transport(value):
    client, calls = exchange()
    with client:
        for method, args in [
            (client.get_offer, (value,)), (client.get_campaign, (value,)),
            (client.list_campaign_streams, (value,)), (client.get_stream, (value,)),
            (client.create_stream, ({"campaign_id": value},)),
            (client.update_stream_offers, (11, value, [])),
            (client.update_stream_offers, (value, 22, [])),
        ]:
            with pytest.raises(KeitaroError, match="invalid_id"):
                method(*args)
    assert calls == []


def test_create_payloads_and_write_order():
    campaign = {"name": CAMPAIGN["name"], "group_id": "102", "domain_id": 101}
    stream = {
        "campaign_id": 11, "action_payload": "https://example.test/",
        "filters": [{"name": "country", "payload": ["US"]}],
        "offers": [{"offer_id": 33, "state": "active", "share": 100}],
    }
    client, calls = exchange(CAMPAIGN, CAMPAIGN, STREAM)
    with client:
        assert client.create_campaign(campaign) == CAMPAIGN
        assert client.create_stream(stream) == STREAM
    assert [(r.method, str(r.url)) for r in calls] == [
        ("POST", BASE + "/campaigns"), ("GET", BASE + "/campaigns/11"),
        ("POST", BASE + "/streams"),
    ]
    for request, payload in [(calls[0], campaign), (calls[2], stream)]:
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == payload


@pytest.mark.parametrize("name", [None, "", "test_other_name", "test_example_", "test_example_  "])
def test_create_campaign_rejects_name(name):
    client, calls = exchange()
    with client, pytest.raises(KeitaroError, match="write_forbidden"):
        client.create_campaign({"name": name})
    assert calls == []


@pytest.mark.parametrize("method", ["create_stream", "update_stream_offers"])
@pytest.mark.parametrize("campaign", [
    {}, {"id": 11}, {"id": 11, "name": 10}, {"id": 11, "name": "foreign"},
    dict(CAMPAIGN, id=True), dict(CAMPAIGN, id=12), httpx.Response(404),
])
def test_campaign_guard(method, campaign):
    client, calls = exchange(campaign)
    with client, pytest.raises(KeitaroError) as caught:
        if method == "create_stream":
            client.create_stream({"campaign_id": 11})
        else:
            client.update_stream_offers(11, 22, [])
    assert [r.method for r in calls] == ["GET"]
    assert not caught.value.outcome_unknown


@pytest.mark.parametrize("stream", [STREAM, {"id": 22}])
def test_update_replaces_only_offers(stream):
    target = [{"offer_id": 33, "share": 100, "state": "active"}]
    row = dict(target[0], id=1, stream_id=22, created_at="old", updated_at="old")
    client, calls = exchange(CAMPAIGN, [{"id": 22}], stream, STREAM)
    with client:
        assert client.update_stream_offers(11, 22, [row]) == STREAM
    assert [(r.method, str(r.url)) for r in calls] == [
        ("GET", BASE + "/campaigns/11"), ("GET", BASE + "/campaigns/11/streams"),
        ("GET", BASE + "/streams/22"), ("PUT", BASE + "/streams/22"),
    ]
    assert json.loads(calls[-1].content) == {"offers": target}
    assert calls[-1].headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("listing,detail", [
    ([], None), ([{"id": 23}], None), ([STREAM, STREAM], None),
    ([dict(STREAM, campaign_id=12)], None), ([STREAM], {"id": 22, "campaign_id": None}),
    ([STREAM], {"id": 22, "campaign_id": 12}), ([STREAM], {"id": "22"}),
    ([STREAM], {}), ([STREAM], httpx.Response(403)),
])
def test_stream_guard(listing, detail):
    client, calls = exchange(CAMPAIGN, listing, detail)
    with client, pytest.raises(KeitaroError):
        client.update_stream_offers(11, 22, [])
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500, 302])
def test_http_errors_and_redirects(status):
    response = httpx.Response(
        status, text=MARKER, headers={"Location": f"https://other.test/{MARKER}"}
    )
    client, calls = exchange(response)
    with client, pytest.raises(KeitaroError) as caught:
        client.list_offers()
    assert caught.value.category == "http"
    assert caught.value.status_code == status
    assert len(calls) == 1
    assert "other.test" not in str(calls[0].url)
    assert MARKER not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("method,reply", [
    ("list_offers", {}), ("list_offers", [{}, 1]), ("get_offer", []),
    ("get_offer", httpx.Response(200, content=b'{"share":NaN}')),
    ("list_offers", httpx.Response(200, content=MARKER.encode())),
])
def test_protocol_errors(method, reply):
    client, calls = exchange(reply)
    with client, pytest.raises(KeitaroError, match="protocol") as caught:
        getattr(client, method)(*([1] if method == "get_offer" else []))
    assert not caught.value.outcome_unknown
    assert MARKER not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error,category", [
    (httpx.ReadTimeout(MARKER), "timeout"), (httpx.ConnectError(MARKER), "transport"),
])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
def test_network_errors_are_safe_and_not_retried(error, category, method):
    prerequisites = [CAMPAIGN, [STREAM], STREAM] if method == "PUT" else []
    client, calls = exchange(*prerequisites, error)
    with client, pytest.raises(KeitaroError) as caught:
        if method == "GET":
            client.list_campaigns()
        elif method == "POST":
            client.create_campaign({"name": CAMPAIGN["name"]})
        else:
            client.update_stream_offers(11, 22, [])
    assert caught.value.category == category
    assert caught.value.outcome_unknown == (method != "GET")
    assert len(calls) == len(prerequisites) + 1
    assert calls[-1].method == method
    assert MARKER not in repr(caught.value) + "".join(traceback.format_exception(caught.value))


def test_closed_client():
    client, calls = exchange([])
    with client:
        assert client.list_offers() == []
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.list_offers()
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [
    ("domain_id", True), ("traffic_source_id", 0), ("group_id", "000"),
    ("group_id", "1/2"), ("group_id", False),
])
def test_invalid_campaign_reference_ids(field, value):
    client, calls = exchange()
    with client, pytest.raises(KeitaroError, match="invalid_id"):
        client.create_campaign({"name": CAMPAIGN["name"], field: value})
    assert calls == []


@pytest.mark.parametrize("offers", [None, [1], [{"offer_id": True}], [{"offer_id": 33}]])
def test_invalid_binding_is_not_published(offers):
    client, calls = exchange()
    with client, pytest.raises(KeitaroError):
        client.update_stream_offers(11, 22, offers)
    assert calls == []


def test_invalid_success_response_does_not_confirm_write():
    client, calls = exchange(httpx.Response(201, content=MARKER.encode()))
    with client, pytest.raises(KeitaroError) as caught:
        client.create_campaign({"name": CAMPAIGN["name"]})
    assert caught.value.category == "protocol" and caught.value.outcome_unknown
    assert MARKER not in repr(caught.value)
    assert [r.method for r in calls] == ["POST"]
