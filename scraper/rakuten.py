"""Rakuten (rakuten.com, US) — all stores with current cash-back rates.

Source: the public "All Stores" feed that powers https://www.rakuten.com/stores/all.
It is paginated 36 stores at a time; ~90 requests cover every store.
"""
from __future__ import annotations

import re
import time

from .common import FetchError, fetch_json, log, slug_of_url

BASE = "https://www.rakuten.com"
TOPIC_ID = "16867"  # the "All Stores" topic behind /stores/all
HEADERS = {"X-Platform": "DESKTOP_WEB"}  # required by the feed API; identifies the site variant
PAGE_DELAY = 0.35  # seconds between pages — be gentle
MAX_PAGES = 400

_REWARD_RE = re.compile(r"^\s*(?P<upto>up to\s+)?(?P<usd>\$)?(?P<num>[\d.,]+)(?P<pct>%)?\s+cash\s+back\s*$", re.I)
_WAS_RE = re.compile(r"^\s*was\s+(?P<usd>\$)?(?P<num>[\d.,]+)(?P<pct>%)?\s*$", re.I)


def parse_reward(text: str | None) -> tuple[float | None, str | None, bool]:
    """'Up to 8% Cash Back' -> (8.0, 'pct', True); '$10 Cash Back' -> (10.0, 'usd', False)."""
    if not text:
        return None, None, False
    m = _REWARD_RE.match(text)
    if not m:
        return None, None, False
    num = float(m.group("num").replace(",", ""))
    kind = "usd" if m.group("usd") else "pct"
    return num, kind, bool(m.group("upto"))


def parse_was(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    m = _WAS_RE.match(text)
    if not m:
        return None, None
    return float(m.group("num").replace(",", "")), ("usd" if m.group("usd") else "pct")


def normalise(item: dict) -> dict | None:
    d = item.get("itemData") or {}
    sid = str(d.get("id") or "").strip()
    name = (d.get("merchantname_text") or "").strip()
    if not sid or not name:
        return None
    url = d.get("image_ctaurl") or ""
    if url.startswith("/"):
        url = BASE + url
    rate, kind, up_to = parse_reward(d.get("currentreward_rewardtext"))
    was, was_kind = parse_was(d.get("previousreward_rewardtext"))
    return {
        "id": sid,
        "name": name,
        "slug": slug_of_url(url),
        "url": url,
        "logo": d.get("image_imageurl") or None,
        "text": (d.get("currentreward_rewardtext") or "").strip() or None,
        "rate": rate,
        "kind": kind,
        "up_to": up_to,
        "was": was,
        "was_kind": was_kind,
        "was_text": (d.get("previousreward_rewardtext") or "").strip() or None,
        "boosted": was is not None and rate is not None and (was_kind == kind) and rate > was,
    }


def fetch_all_stores() -> list[dict]:
    """Return every store on Rakuten US. Raises FetchError if the listing cannot be completed,
    so a partial list is never mistaken for stores having disappeared."""
    stores: dict[str, dict] = {}
    cursor: str | None = None
    pages = 0
    while True:
        # alphabetical is a stable order; the default "cash back" sort reshuffles between pages and
        # lets stores slip through the page boundaries.
        url = f"{BASE}/feedapi/v1/regions/USA/topic?topicId={TOPIC_ID}&sort=alphabetical"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url, headers=HEADERS)
        try:
            items = data["data"]["viewer"]["topic"]["items"]
            edges = items["edges"]
            page_info = items["pageInfo"]
        except (KeyError, TypeError) as e:
            raise FetchError(f"unexpected Rakuten feed shape on page {pages + 1}: {e}")
        for edge in edges:
            s = normalise(edge.get("node") or {})
            if s:
                stores[s["id"]] = s
        pages += 1
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor or pages >= MAX_PAGES:
            raise FetchError("Rakuten pagination did not terminate")
        time.sleep(PAGE_DELAY)
    log(f"rakuten: {len(stores)} stores from {pages} pages")
    if len(stores) < 1000:
        # Rakuten lists ~3,000+ stores; far fewer means the feed is broken or truncated.
        raise FetchError(f"Rakuten returned only {len(stores)} stores — refusing to treat as complete")
    return list(stores.values())
