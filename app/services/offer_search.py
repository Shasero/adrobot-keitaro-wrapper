from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from pydantic import BaseModel

from app.integrations.keitaro import KeitaroClient
from app.services.campaigns import offers_by_id


class Offer(BaseModel):
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class CatalogOffer:
    id: int
    name: str
    state: str


class OfferSearchCache:
    """Cache only validated search data. Write checks use offers_by_id directly."""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = monotonic):
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._catalog: tuple[CatalogOffer, ...] = ()
        self._expires_at: float | None = None
        self._pending: Future[tuple[CatalogOffer, ...]] | None = None

    def _load(self, connect: Callable[[], KeitaroClient]) -> tuple[CatalogOffer, ...]:
        with self._lock:
            if self._expires_at is not None and self._clock() < self._expires_at:
                return self._catalog
            pending = self._pending
            if pending is None:
                self._pending = Future()
                refresh = self._pending

        if pending is not None:
            return pending.result()

        # The first request loads outside the lock. Concurrent requests share
        # its result or error. A later request can retry a failed refresh.
        try:
            catalog = tuple(
                CatalogOffer(key, row["name"], row["state"])
                for key, row in offers_by_id(connect()).items()
            )
            expires_at = self._clock() + self._ttl_seconds
        except BaseException as exc:
            with self._lock:
                self._pending = None
                refresh.set_exception(exc)
            raise

        with self._lock:
            self._catalog = catalog
            self._expires_at = expires_at
            self._pending = None
            refresh.set_result(catalog)
        return catalog

    def search(self, connect: Callable[[], KeitaroClient], query: str, limit: int) -> list[Offer]:
        query = query.strip().casefold()
        matches = []
        for offer in self._load(connect):
            if offer.state == "active" and (query in offer.name.casefold() or query == str(offer.id)):
                matches.append(Offer(id=offer.id, name=offer.name))
                if len(matches) == limit:
                    break
        return matches
