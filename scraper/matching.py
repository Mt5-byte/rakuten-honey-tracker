"""Pair Rakuten stores with their Honey counterparts.

Strategy, cheapest first:
  1. manual pairs from config.json
  2. pairs remembered in data/store_map.json from earlier runs
  3. exact match on normalised name or slug against Honey's top-stores list
  4. a direct Honey lookup by slug (web_getStoreByLabel), remembered afterwards —
     misses are remembered too and only retried every N days.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import honey
from .common import FetchError, log, norm_name, now_ts, similar

LOOKUP_THREADS = 4


def _label_candidates(rak: dict) -> list[str]:
    slug = rak.get("slug") or ""
    name = rak.get("name") or ""
    cands: list[str] = []
    if slug:
        cands.append(slug)
        s2 = re.sub(r"^(us-|usa-)", "", slug)
        s2 = re.sub(r"(-us|-usa|-com)$", "", s2)
        if s2 and s2 != slug:
            cands.append(s2)
    hy = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " and ")).strip("-")
    hy = re.sub(r"\.(com|net|org|co|us)$", "", hy)
    if hy:
        cands.append(hy)
        compact = hy.replace("-", "")
        if compact != hy:
            cands.append(compact)
    seen: set[str] = set()
    out = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:4]


def _lookup_one(rak: dict) -> tuple[str, dict | None, list[str]]:
    tried: list[str] = []
    for label in _label_candidates(rak):
        tried.append(label)
        try:
            raw = honey.lookup_by_label(label)
        except FetchError as e:
            log(f"lookup error for {label}: {e}")
            continue
        if raw and not honey.is_junk(raw) and similar(rak["name"], raw.get("name")):
            return rak["id"], raw, tried
        time.sleep(0.15)
    return rak["id"], None, tried


def pair_stores(rak_stores: list[dict], honey_top_raw: list[dict], store_map: dict, config: dict,
                max_lookups: int) -> dict:
    """Returns {'pairs': {rak_id: honey_raw}, 'honey_only': [honey_raw...], 'stats': {...}}
    and mutates store_map in place."""
    now = now_ts()
    lookup_cfg = config.get("lookup") or {}
    retry_days = float(lookup_cfg.get("retry_misses_days", 14))
    manual = {k.lower(): v.lower() for k, v in (config.get("manual_pairs") or {}).items()}
    never = {s.lower() for s in (config.get("never_pair") or [])}

    store_map.setdefault("pairs", {})
    store_map.setdefault("misses", {})
    store_map.setdefault("extras", {})
    pairs_map: dict = store_map["pairs"]
    misses: dict = store_map["misses"]
    extras: dict = store_map["extras"]

    top: dict[str, dict] = {}
    by_name: dict[str, set[str]] = {}
    by_label: dict[str, set[str]] = {}
    for raw in honey_top_raw:
        if honey.is_junk(raw):
            continue
        top[str(raw["storeId"])] = raw
    # Honey's list order is live-scored and shuffles between page requests; previously seen stores
    # that fell through a page boundary are fetched back by id.
    seen_cache = store_map.setdefault("honey_seen", {})
    for sid, raw in honey.recover_missing(top, seen_cache, now).items():
        if not honey.is_junk(raw):
            top[sid] = raw
    honey.remember_seen(seen_cache, top, now)
    for sid, raw in top.items():
        by_name.setdefault(norm_name(raw.get("name")), set()).add(sid)
        by_label.setdefault(norm_name(raw.get("label")), set()).add(sid)
        by_label.setdefault(re.sub(r"[^a-z0-9]", "", (raw.get("label") or "").lower()), set()).add(sid)

    paired: dict[str, str] = {}  # rak_id -> honey_id
    need_lookup: list[dict] = []
    stats = {"manual": 0, "remembered": 0, "name": 0, "lookup_hit": 0, "lookup_miss": 0, "skipped_recent_miss": 0,
             "ambiguous": 0}

    for rak in rak_stores:
        rid, slug = rak["id"], rak.get("slug", "").lower()
        if slug in never:
            continue
        # 1. manual
        if slug in manual:
            want = manual[slug]
            hid = next((sid for sid, r in top.items() if (r.get("label") or "").lower() == want), None)
            if hid is None:
                hid = next((hid for hid, e in extras.items() if e.get("label") == want), None)
            if hid is None:
                try:
                    raw = honey.lookup_by_label(want)
                except FetchError:
                    raw = None
                if raw:
                    hid = str(raw["storeId"])
                    extras[hid] = {"label": raw["label"], "name": raw["name"], "raw": _slim(raw), "ts": now}
            if hid:
                paired[rid] = hid
                pairs_map[rid] = {"honey_id": hid, "how": "manual", "ts": now}
                stats["manual"] += 1
                continue
        # 2. remembered
        pm = pairs_map.get(rid)
        if pm and pm.get("how") != "manual":
            hid = pm["honey_id"]
            if hid in top or hid in extras:
                paired[rid] = hid
                stats["remembered"] += 1
                continue
            pairs_map.pop(rid, None)  # honey store vanished; re-match below
        elif pm and pm.get("how") == "manual" and slug not in manual:
            pairs_map.pop(rid, None)
        # 3. exact normalised match against the top list
        keys = {norm_name(rak["name"]), norm_name(slug), re.sub(r"[^a-z0-9]", "", slug)}
        cands: set[str] = set()
        for k in keys:
            if k:
                cands |= by_name.get(k, set())
                cands |= by_label.get(k, set())
        if len(cands) > 1:
            exact = {c for c in cands if (top[c].get("label") or "").lower() == slug}
            if len(exact) == 1:
                cands = exact
            else:
                stats["ambiguous"] += 1
        if len(cands) == 1:
            hid = cands.pop()
            paired[rid] = hid
            pairs_map[rid] = {"honey_id": hid, "how": "name", "ts": now}
            stats["name"] += 1
            continue
        # 4. lookup (rate-limited by remembered misses)
        miss = misses.get(rid)
        if miss and (now - miss.get("ts", 0)) < retry_days * 86400:
            stats["skipped_recent_miss"] += 1
            continue
        need_lookup.append(rak)

    if need_lookup:
        todo = need_lookup[:max_lookups]
        log(f"matching: {len(need_lookup)} stores need a Honey lookup; doing {len(todo)} this run")
        with ThreadPoolExecutor(max_workers=LOOKUP_THREADS) as ex:
            for rid, raw, tried in ex.map(_lookup_one, todo):
                if raw:
                    hid = str(raw["storeId"])
                    paired[rid] = hid
                    pairs_map[rid] = {"honey_id": hid, "how": "lookup", "ts": now}
                    misses.pop(rid, None)
                    if hid not in top:
                        extras[hid] = {"label": raw["label"], "name": raw["name"], "raw": _slim(raw), "ts": now}
                    stats["lookup_hit"] += 1
                else:
                    misses[rid] = {"ts": now, "tried": tried}
                    stats["lookup_miss"] += 1

    # Refresh extras (Honey stores not in the top list) in bulk, then fetch detail only for changed offers.
    extra_ids = sorted({hid for hid in paired.values() if hid not in top})
    extras_raw: dict[str, dict] = {}
    if extra_ids:
        try:
            fetched = honey.fetch_by_ids(extra_ids)
        except FetchError as e:
            log(f"extras refresh failed: {e}; using cached records")
            fetched = []
        fetched_by_id = {str(r["storeId"]): r for r in fetched if r and r.get("storeId")}
        need_detail: list[str] = []
        for hid in extra_ids:
            cache = extras.setdefault(hid, {})
            r = fetched_by_id.get(hid)
            if not r:
                if cache.get("raw"):
                    extras_raw[hid] = cache["raw"]
                continue
            gold = r.get("gold") or {}
            cached_raw = cache.get("raw") or {}
            cached_gold = cached_raw.get("gold") or {}
            same_offer = (gold.get("started") == cached_gold.get("started") and "fixedRatePercent" in cached_gold) \
                or (not gold and not cached_gold)
            if same_offer and gold.get("active") == cached_gold.get("active"):
                # carry the exact fixed rate forward; everything else from the fresh record
                merged = dict(r)
                merged["gold"] = dict(gold, fixedRatePercent=cached_gold.get("fixedRatePercent")) if gold else None
                merged["previousOffer"] = cached_raw.get("previousOffer")
                extras_raw[hid] = merged
            else:
                need_detail.append(hid)
                extras_raw[hid] = r  # provisional
        if need_detail:
            log(f"matching: {len(need_detail)} extra stores changed offers; fetching detail")

            def _detail(hid: str):
                label = (extras_raw[hid].get("label") or extras.get(hid, {}).get("label"))
                try:
                    return hid, (honey.lookup_by_label(label) if label else None)
                except FetchError as e:
                    log(f"detail error {label}: {e}")
                    return hid, None

            with ThreadPoolExecutor(max_workers=LOOKUP_THREADS) as ex:
                for hid, raw in ex.map(_detail, need_detail):
                    if raw:
                        prev_cached = (extras.get(hid) or {}).get("raw") or {}
                        if prev_cached.get("gold") and prev_cached["gold"].get("started") != (raw.get("gold") or {}).get("started"):
                            raw = dict(raw, previousOffer=prev_cached.get("gold"))
                        extras_raw[hid] = raw
        for hid, raw in extras_raw.items():
            extras[hid] = {"label": raw.get("label"), "name": raw.get("name"), "raw": _slim(raw), "ts": now}

    # drop extras no longer referenced by any pair
    referenced = set(paired.values())
    for hid in [h for h in extras if h not in referenced]:
        extras.pop(hid, None)

    pairs_out: dict[str, dict] = {}
    for rid, hid in paired.items():
        raw = top.get(hid) or extras_raw.get(hid) or (extras.get(hid) or {}).get("raw")
        if raw:
            pairs_out[rid] = raw
    paired_honey = set(paired.values())
    honey_only = [raw for sid, raw in top.items() if sid not in paired_honey]
    stats["pairs"] = len(pairs_out)
    stats["honey_only"] = len(honey_only)
    stats["extras"] = len(extras)
    log(f"matching: {stats}")
    return {"pairs": pairs_out, "honey_only": honey_only, "stats": stats}


def _slim(raw: dict) -> dict:
    """Keep only what normalise() needs so store_map.json stays small."""
    keep = ("storeId", "label", "name", "url", "country", "supported", "active", "gold", "previousOffer",
            "publicCouponCount")
    return {k: raw.get(k) for k in keep if k in raw}
