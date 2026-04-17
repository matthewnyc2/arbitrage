"""Polymarket Gamma REST client.

Paginates /events, filters to active negRisk categoricals, and normalizes each
event into our pydantic `Event` model. The shape of a Gamma event is documented
in docs/api/negrisk.md; we read only the fields we need and ignore the rest.

Refresh cadence and persistence live here too — the whole subsystem is kept in
one file since the only consumer is the opportunity engine.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings
from ..db import db_conn
from ..models import Event, Outcome

GAMMA_EVENTS_PATH = "/events"
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_S = 20.0


@dataclass(slots=True)
class DiscoveryStats:
    pages_fetched: int = 0
    events_seen: int = 0
    neg_risk_events: int = 0
    upserted: int = 0
    skipped_inactive: int = 0
    skipped_not_neg_risk: int = 0
    skipped_malformed: int = 0


class GammaClient:
    """Thin async wrapper around Polymarket's Gamma REST API."""

    def __init__(
        self,
        *,
        host: str | None = None,
        client: httpx.AsyncClient | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._host = (host or settings.gamma_host).rstrip("/")
        self._page_size = page_size
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._host,
            timeout=timeout_s,
            headers={"accept": "application/json"},
        )

    async def __aenter__(self) -> GammaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def iter_active_event_pages(
        self,
        *,
        order: str = "volume24hr",
        ascending: bool = False,
        max_pages: int = 50,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield each page of raw event dicts until a short page or max_pages."""
        offset = 0
        for _ in range(max_pages):
            params = {
                "closed": "false",
                "archived": "false",
                "active": "true",
                "limit": self._page_size,
                "offset": offset,
                "order": order,
                "ascending": "true" if ascending else "false",
            }
            page = await self._get_json(GAMMA_EVENTS_PATH, params)
            if not isinstance(page, list):
                logger.warning("Gamma /events returned non-list: {!r}", type(page))
                return
            if not page:
                return
            yield page
            if len(page) < self._page_size:
                return
            offset += self._page_size

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, max=5.0),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                return orjson.loads(resp.content)
        raise RuntimeError("unreachable")


def normalize_event(raw: dict[str, Any]) -> Event | None:
    """Translate a raw Gamma event dict into our `Event` model.

    Returns None if the event is not an active negRisk categorical, or if its
    required fields are missing/malformed.
    """
    if not raw.get("negRisk"):
        return None
    markets = raw.get("markets") or []
    if len(markets) < 2:
        return None

    event_id = raw.get("negRiskMarketID") or str(raw.get("id") or "")
    if not event_id:
        return None

    outcomes: list[Outcome] = []
    for idx, m in enumerate(markets):
        if m.get("closed") or m.get("archived"):
            return None
        token_ids_raw = m.get("clobTokenIds")
        if isinstance(token_ids_raw, str):
            try:
                token_ids = orjson.loads(token_ids_raw)
            except orjson.JSONDecodeError:
                return None
        else:
            token_ids = token_ids_raw
        if not isinstance(token_ids, list) or len(token_ids) < 1:
            return None
        yes_token_id = str(token_ids[0])
        if not yes_token_id:
            return None
        name = (
            m.get("groupItemTitle")
            or m.get("outcome")
            or m.get("question")
            or f"Outcome {idx + 1}"
        )
        outcomes.append(Outcome(token_id=yes_token_id, name=str(name), outcome_index=idx))

    if len(outcomes) < 2:
        return None

    seen: set[str] = set()
    for o in outcomes:
        if o.token_id in seen:
            return None
        seen.add(o.token_id)

    end_date = _parse_end_date(raw.get("endDate"))

    return Event(
        id=event_id,
        slug=str(raw.get("slug") or event_id),
        title=str(raw.get("title") or raw.get("slug") or event_id),
        is_neg_risk=True,
        end_date=end_date,
        outcomes=tuple(outcomes),
    )


def _parse_end_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def upsert_events(events: Iterable[Event], *, raw_by_id: dict[str, Any] | None = None) -> int:
    """Persist events + outcomes to SQLite. Returns count of events upserted."""
    now = datetime.now(UTC).isoformat()
    raw_by_id = raw_by_id or {}
    count = 0
    async with db_conn() as conn:
        for ev in events:
            raw_json = json.dumps(raw_by_id.get(ev.id)) if ev.id in raw_by_id else None
            await conn.execute(
                """
                INSERT INTO events (id, slug, title, is_neg_risk, end_date, active, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  slug=excluded.slug,
                  title=excluded.title,
                  is_neg_risk=excluded.is_neg_risk,
                  end_date=excluded.end_date,
                  active=1,
                  raw_json=COALESCE(excluded.raw_json, events.raw_json),
                  updated_at=excluded.updated_at
                """,
                (
                    ev.id,
                    ev.slug,
                    ev.title,
                    1 if ev.is_neg_risk else 0,
                    ev.end_date.isoformat() if ev.end_date else None,
                    raw_json,
                    now,
                ),
            )
            for o in ev.outcomes:
                await conn.execute(
                    """
                    INSERT INTO outcomes (token_id, event_id, name, outcome_index, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(token_id) DO UPDATE SET
                      event_id=excluded.event_id,
                      name=excluded.name,
                      outcome_index=excluded.outcome_index,
                      updated_at=excluded.updated_at
                    """,
                    (o.token_id, ev.id, o.name, o.outcome_index, now),
                )
            count += 1
        await conn.commit()
    return count


async def mark_inactive(kept_event_ids: set[str]) -> int:
    """Mark any previously-active event not in `kept_event_ids` as inactive."""
    if not kept_event_ids:
        return 0
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in kept_event_ids)
    async with db_conn() as conn:
        cursor = await conn.execute(
            f"UPDATE events SET active=0, updated_at=? "
            f"WHERE active=1 AND id NOT IN ({placeholders})",
            (now, *kept_event_ids),
        )
        await conn.commit()
        return cursor.rowcount or 0


async def discover_once(
    *,
    client: GammaClient | None = None,
    max_pages: int = 20,
) -> DiscoveryStats:
    """Single pass: walk Gamma, filter to negRisk, persist, mark drops inactive."""
    stats = DiscoveryStats()
    owned: GammaClient | None = None
    if client is None:
        owned = GammaClient()
        client = owned

    events: list[Event] = []
    raw_by_id: dict[str, Any] = {}
    try:
        async for page in client.iter_active_event_pages(max_pages=max_pages):
            stats.pages_fetched += 1
            for raw in page:
                stats.events_seen += 1
                if not raw.get("negRisk"):
                    stats.skipped_not_neg_risk += 1
                    continue
                stats.neg_risk_events += 1
                if raw.get("closed") or raw.get("archived"):
                    stats.skipped_inactive += 1
                    continue
                ev = normalize_event(raw)
                if ev is None:
                    stats.skipped_malformed += 1
                    continue
                events.append(ev)
                raw_by_id[ev.id] = raw
    finally:
        if owned is not None:
            await owned.aclose()

    stats.upserted = await upsert_events(events, raw_by_id=raw_by_id)
    await mark_inactive({ev.id for ev in events})
    logger.info(
        "discovery: seen={}, negRisk={}, upserted={}, malformed={}",
        stats.events_seen,
        stats.neg_risk_events,
        stats.upserted,
        stats.skipped_malformed,
    )
    return stats


async def discovery_loop(interval_seconds: int = 120) -> None:
    """Run discover_once forever, sleeping between passes."""
    while True:
        try:
            await discover_once()
        except Exception as exc:
            logger.exception("discovery pass failed: {}", exc)
        await asyncio.sleep(interval_seconds)
