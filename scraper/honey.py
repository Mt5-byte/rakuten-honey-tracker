"""Honey (joinhoney.com, US) — stores and their PayPal Rewards / Honey Gold rates.

Source: the public GraphQL endpoint used by joinhoney.com's own web pages
(https://d.joinhoney.com/v3). Three persisted operations are used:
  * web_getTopStores      — all US stores, 500 per page (~10 requests)
  * web_getStoreByLabel   — one store by its /shop/<label> slug (used to find stores that
                            Rakuten lists but the top-stores list does not include)
  * web_getStoresByIds    — refresh many known stores at once (no fixedRatePercent field)
"""
from __future__ import annotations

import json
import time
import urllib.parse

from .common import FetchError, fetch_json, log

GQL = "https://d.joinhoney.com/v3"
SHOP = "https://www.joinhoney.com/shop/"
PAGE_SIZE = 500
MAX_PAGES = 60
BATCH = 200
DELAY = 0.4

POINTS_PER_DOLLAR = 100  # PayPal Rewards: 100 points ≈ $1


def gql(op: str, variables: dict) -> dict:
    url = f"{GQL}?operationName={op}&variables={urllib.parse.quote(json.dumps(variables, separators=(',', ':')))}"
    return fetch_json(url)


PAGE_STEP = 400  # pages overlap by 100 stores: the list is sorted by a live score and shuffles between requests


def fetch_top_stores() -> list[dict]:
    """All stores Honey lists for the US. Raises FetchError on an incomplete listing."""
    out: dict[str, dict] = {}
    offset = 0
    pages = 0
    while pages < MAX_PAGES:
        d = gql("web_getTopStores", {"limit": PAGE_SIZE, "country": "US", "offset": offset})
        stores = (d.get("data") or {}).get("getTopStores")
        if stores is None:
            raise FetchError(f"honey top-stores: unexpected response {json.dumps(d)[:200]}")
        if not stores:
            break
        for s in stores:
            sid = str(s.get("storeId") or "")
            if sid:
                out[sid] = s
        offset += PAGE_STEP
        pages += 1
        time.sleep(DELAY)
    log(f"honey: {len(out)} stores from {pages} pages")
    if len(out) < 1500:
        raise FetchError(f"Honey returned only {len(out)} stores — refusing to treat as complete")
    return list(out.values())


def recover_missing(top: dict[str, dict], seen: dict, now: int, max_detail: int = 150) -> dict[str, dict]:
    """Stores that were in Honey's list before but fell off this run's pages (the list's order is
    not stable) are re-fetched by id. Returns {store_id: raw} for the ones Honey still has.
    `seen` is the cache {store_id: {label, started, fixed, active, prev, last}} kept in store_map."""
    missing = [sid for sid, c in seen.items() if sid not in top and now - c.get("last", 0) < 60 * 86400]
    if not missing:
        return {}
    try:
        fetched = fetch_by_ids(missing)
    except FetchError as e:
        log(f"honey recovery failed: {e}")
        return {}
    recovered: dict[str, dict] = {}
    need_detail: list[str] = []
    for raw in fetched:
        sid = str(raw.get("storeId") or "")
        if not sid or sid not in seen:
            continue
        c = seen[sid]
        gold = raw.get("gold")
        if gold is not None:
            if gold.get("started") == c.get("started") and gold.get("active") == c.get("active"):
                gold = dict(gold, fixedRatePercent=c.get("fixed"))
            else:
                need_detail.append(sid)
            raw = dict(raw, gold=gold)
        raw["previousOffer"] = c.get("prev")
        recovered[sid] = raw
    for sid in need_detail[:max_detail]:
        label = recovered[sid].get("label") or seen[sid].get("label")
        try:
            detail = lookup_by_label(label) if label else None
        except FetchError as e:
            log(f"honey recovery detail {label}: {e}")
            detail = None
        if detail:
            prev = seen[sid].get("prev")
            cur_started = (detail.get("gold") or {}).get("started")
            if seen[sid].get("started") and seen[sid]["started"] != cur_started:
                prev = {"storeId": sid, "started": seen[sid]["started"] * 1000 if seen[sid]["started"] < 10 ** 11 else seen[sid]["started"],
                        "active": seen[sid].get("active"), "fixedRatePercent": seen[sid].get("fixed"),
                        "maxPercent": seen[sid].get("fixed"), "minPercent": seen[sid].get("fixed")}
            recovered[sid] = dict(detail, previousOffer=prev)
        time.sleep(0.15)
    log(f"honey: recovered {len(recovered)} of {len(missing)} stores missing from the list ({len(need_detail)} needed detail)")
    return recovered


def remember_seen(seen: dict, raws: dict[str, dict], now: int) -> None:
    """Update the seen-cache from this run's raw records and forget stores gone for 60+ days."""
    for sid, raw in raws.items():
        gold = raw.get("gold") or {}
        prev = raw.get("previousOffer")
        seen[sid] = {
            "label": raw.get("label"), "started": gold.get("started"), "fixed": gold.get("fixedRatePercent"),
            "active": gold.get("active"), "prev": _slim_gold(prev) if prev else None, "last": now,
        }
    for sid in [s for s, c in seen.items() if now - c.get("last", 0) >= 60 * 86400]:
        seen.pop(sid, None)


def _slim_gold(g: dict) -> dict:
    keep = ("storeId", "started", "active", "fixedRatePercent", "maxPercent", "minPercent", "isFlatFee", "minFlatFee", "maxFlatFee")
    return {k: g.get(k) for k in keep if k in g}


def lookup_by_label(label: str) -> dict | None:
    """Fetch one store by slug; None when Honey has no such store."""
    d = gql("web_getStoreByLabel", {"label": label})
    store = (d.get("data") or {}).get("getStoreByLabel")
    if store:
        return store
    errs = d.get("errors") or []
    if any((e.get("extensions") or {}).get("code") == "NotFound" or "not found" in (e.get("message") or "").lower()
           for e in errs):
        return None
    if errs:
        raise FetchError(f"honey lookup {label}: {errs[0].get('message')}")
    return None


def fetch_by_ids(ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        d = gql("web_getStoresByIds", {"storeIds": chunk})
        stores = (d.get("data") or {}).get("getStoresByIds")
        if stores is None:
            raise FetchError(f"honey by-ids: unexpected response {json.dumps(d)[:200]}")
        out.extend(s for s in stores if s)
        if i + BATCH < len(ids):
            time.sleep(DELAY)
    return out


def is_junk(raw: dict) -> bool:
    name = (raw.get("name") or "").strip()
    label = (raw.get("label") or "").strip().lower()
    up = name.upper()
    if not name or not label:
        return True
    if up.startswith(("DUPLICATE", "TEST ", "TEST-", "(OLD)", "OLD ")) or "duplicate" in label:
        return True
    if raw.get("supported") is False or raw.get("active") is False:
        return True
    if raw.get("country") and raw.get("country") != "US":
        return True
    return False


def _fmt(n: float) -> str:
    return f"{n:g}"


def parse_offer(gold: dict | None) -> dict:
    """Turn Honey's gold/offer record into (rate, kind, up_to, min, text)."""
    gold = gold or {}
    active = bool(gold.get("active"))
    fixed = gold.get("fixedRatePercent")
    has_fixed_field = "fixedRatePercent" in gold
    maxp = gold.get("maxPercent") or 0
    minp = gold.get("minPercent") or 0
    res = {
        "rate": None, "kind": None, "up_to": False, "min": None, "text": "No rewards",
        "active": active, "started": int(gold["started"] / 1000) if gold.get("started") else None,
        "has_fixed_field": has_fixed_field,
    }
    if not gold or not active:
        return res
    if fixed is not None and fixed > 0:
        res.update(rate=float(fixed), kind="pct", text=f"{_fmt(fixed)}% back")
    elif gold.get("isFlatFee") and (gold.get("maxFlatFee") or 0) > 0:
        mn, mx = int(gold.get("minFlatFee") or 0), int(gold.get("maxFlatFee") or 0)
        res.update(rate=float(mx), kind="pts", min=float(mn), up_to=mn != mx,
                   text=(f"{mn:,}–{mx:,} points" if mn != mx else f"{mx:,} points"))
    elif maxp > 0:
        res.update(rate=float(maxp), kind="pct", up_to=True, min=float(minp) if minp else None,
                   text=f"Up to {_fmt(maxp)}% back")
    else:
        res.update(text="No rewards")
    return res


def normalise(raw: dict) -> dict | None:
    sid = str(raw.get("storeId") or "").strip()
    label = (raw.get("label") or "").strip()
    name = (raw.get("name") or "").strip()
    if not sid or not label or not name:
        return None
    cur = parse_offer(raw.get("gold"))
    prev_raw = raw.get("previousOffer")
    prev = parse_offer(prev_raw) if prev_raw else None
    boosted = False
    if prev and prev["rate"] is not None and cur["rate"] is not None and prev["kind"] == cur["kind"] == "pct":
        recent = cur["started"] is not None and (time.time() - cur["started"]) < 45 * 86400
        meaningful = cur["rate"] >= prev["rate"] * 1.25 or cur["rate"] - prev["rate"] >= 2
        boosted = cur["rate"] > prev["rate"] and recent and meaningful
    return {
        "id": sid,
        "label": label,
        "name": name,
        "url": SHOP + label,
        "site": raw.get("url") or None,
        "rate": cur["rate"],
        "kind": cur["kind"],
        "up_to": cur["up_to"],
        "min": cur["min"],
        "text": cur["text"],
        "active": cur["active"],
        "started": cur["started"],
        "has_fixed_field": cur["has_fixed_field"],
        "prev_rate": prev["rate"] if prev else None,
        "prev_kind": prev["kind"] if prev else None,
        "prev_started": prev["started"] if prev else None,
        "boosted": boosted,
        "coupons": raw.get("publicCouponCount"),
    }
