"""Synchronous Keitaro Admin API client. Returned records are private service data."""

import math
import re
from collections.abc import Callable
from typing import Any, Self

import httpx
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from app.core.config import Settings

type Record = dict[str, Any]


class KeitaroError(Exception):
    def __init__(
        self, category: str, *, status_code: int | None = None, outcome_unknown: bool = False
    ):
        self.category = category
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        message = f"Keitaro: {category}"
        if status_code is not None:
            message += f" (HTTP {status_code})"
        super().__init__(message)


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise KeitaroError("invalid_id")
    return value


def _matches_id(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _reject_nonfinite_json(_: str) -> None:
    raise ValueError("Invalid JSON number")


def _base_url(raw: str) -> str:
    if not re.fullmatch(r"(?i:https)://[^/?#@\\\s]+/admin_api/v1/?", raw):
        raise KeitaroError("configuration")
    try:
        url = AnyHttpUrl(raw)
    except ValidationError:
        raise KeitaroError("configuration") from None
    if not url.host or url.host.rstrip(".").lower() == "example.invalid":
        raise KeitaroError("configuration")
    return str(url).rstrip("/") + "/"


class KeitaroClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        base_url = _base_url(settings.keitaro_base_url)
        secret = settings.keitaro_api_key
        key = secret.get_secret_value() if isinstance(secret, SecretStr) else ""
        timeout = settings.keitaro_timeout_seconds
        self._prefix = settings.campaign_name_prefix
        if not re.fullmatch(r"[!-~]+", key):
            raise KeitaroError("configuration")
        if not math.isfinite(timeout) or timeout <= 0:
            raise KeitaroError("configuration")
        if not re.fullmatch(r"test_[A-Za-z]+(?:-[A-Za-z]+)*_", self._prefix):
            raise KeitaroError("configuration")
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Api-Key": key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request[T](
        self, method: str, path: str, shape: type[T], *,
        params: dict[str, str] | None = None, body: Record | None = None,
    ) -> T:
        writing = method in ("POST", "PUT")
        try:
            request = self._http.build_request(method, path, params=params, json=body)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise KeitaroError("invalid_payload") from None
        try:
            response = self._http.send(request)
        except httpx.TimeoutException:
            raise KeitaroError("timeout", outcome_unknown=writing) from None
        except (httpx.DecodingError, httpx.InvalidURL):
            raise KeitaroError("protocol", outcome_unknown=writing) from None
        except httpx.RequestError:
            raise KeitaroError("transport", outcome_unknown=writing) from None
        if not response.is_success:
            raise KeitaroError(
                "http", status_code=response.status_code,
                outcome_unknown=writing and (response.is_server_error or response.status_code == 408),
            )
        try:
            data = response.json(parse_constant=_reject_nonfinite_json)
        except (ValueError, RecursionError):
            raise KeitaroError("protocol", outcome_unknown=writing) from None
        if not isinstance(data, shape) or (
            isinstance(data, list) and not all(isinstance(row, dict) for row in data)
        ):
            raise KeitaroError("protocol", outcome_unknown=writing)
        return data

    def list_domains(self) -> list[Record]:
        return self._request("GET", "domains", list)

    def list_campaign_groups(self) -> list[Record]:
        return self._request("GET", "groups", list, params={"type": "campaigns"})

    def list_traffic_sources(self) -> list[Record]:
        return self._request("GET", "traffic_sources", list)

    def list_offers(self) -> list[Record]:
        return self._request("GET", "offers", list)

    def get_offer(self, offer_id: int) -> Record:
        return self._request("GET", f"offers/{_positive_id(offer_id)}", dict)

    def list_campaigns(self) -> list[Record]:
        return self._request("GET", "campaigns", list)

    def get_campaign(self, campaign_id: int) -> Record:
        return self._request("GET", f"campaigns/{_positive_id(campaign_id)}", dict)

    def list_campaign_streams(self, campaign_id: int) -> list[Record]:
        return self._request("GET", f"campaigns/{_positive_id(campaign_id)}/streams", list)

    def get_stream(self, stream_id: int) -> Record:
        return self._request("GET", f"streams/{_positive_id(stream_id)}", dict)

    def _check_name(self, name: object) -> None:
        if not isinstance(name, str) or not name.startswith(self._prefix):
            raise KeitaroError("write_forbidden")
        if not name[len(self._prefix):].strip():
            raise KeitaroError("write_forbidden")

    def _check_campaign(self, campaign_id: int) -> None:
        campaign = self.get_campaign(campaign_id)
        if not _matches_id(campaign.get("id"), campaign_id):
            raise KeitaroError("write_forbidden")
        self._check_name(campaign.get("name"))

    def create_campaign(self, payload: Record) -> Record:
        body = payload.copy()
        self._check_name(body.get("name"))
        for field in ("domain_id", "traffic_source_id", "group_id"):
            if field in body:
                value = body[field]
                if field == "group_id" and isinstance(value, str):
                    if not re.fullmatch(r"[0-9]+", value) or not value.strip("0"):
                        raise KeitaroError("invalid_id")
                else:
                    _positive_id(value)
        return self._request("POST", "campaigns", dict, body=body)

    def create_stream(self, payload: Record, *, before_post: Callable[[], None] | None = None) -> Record:
        body = payload.copy()
        campaign_id = _positive_id(body.get("campaign_id"))
        if "offers" in body:
            _check_offer_ids(body["offers"])
        self._check_campaign(campaign_id)
        if before_post is not None:
            before_post()
        return self._request("POST", "streams", dict, body=body)

    def update_stream_offers(
        self, campaign_id: int, stream_id: int, offers: list[Record]
    ) -> Record:
        _positive_id(stream_id)
        _check_offer_ids(offers)
        fields = ("offer_id", "share", "state")
        try:
            target = [{field: row[field] for field in fields} for row in offers]
        except KeyError:
            raise KeitaroError("invalid_payload") from None

        self._check_campaign(campaign_id)
        streams = self.list_campaign_streams(campaign_id)
        matches = [row for row in streams if _matches_id(row.get("id"), stream_id)]
        if len(matches) != 1:
            raise KeitaroError("write_forbidden")
        _check_stream_campaign(matches[0], campaign_id)
        stream = self.get_stream(stream_id)
        if not _matches_id(stream.get("id"), stream_id):
            raise KeitaroError("write_forbidden")
        _check_stream_campaign(stream, campaign_id)
        return self._request("PUT", f"streams/{stream_id}", dict, body={"offers": target})


def _check_offer_ids(offers: object) -> None:
    if not isinstance(offers, list):
        raise KeitaroError("invalid_payload")
    for row in offers:
        if not isinstance(row, dict):
            raise KeitaroError("invalid_payload")
        _positive_id(row.get("offer_id"))


def _check_stream_campaign(stream: Record, campaign_id: int) -> None:
    if "campaign_id" in stream and not _matches_id(stream["campaign_id"], campaign_id):
        raise KeitaroError("write_forbidden")
