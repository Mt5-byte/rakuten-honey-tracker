"""One tracking run: fetch both sites, pair stores, update history, detect spikes, write data/.

    python -m scraper.run --data-dir data --config config.json

Exits non-zero (and writes nothing) if either site could not be listed completely, so a flaky
run can never be mistaken for thousands of stores dropping their rewards.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

from . import alerts as alerts_mod
from . import history as hist_mod
from . import honey, matching, rakuten
from .common import FetchError, iso, load_json, log, now_ts, save_json

VERSION = 1


def _clean(d: dict) -> dict:
    """Drop null/False fields to keep latest.json small."""
    return {k: v for k, v in d.items() if v is not None and v is not False}


LOGO_PREFIX = "https://static.rakuten.com/img/store/"


def _logo(url: str | None) -> str | None:
    if not url:
        return None
    return url[len(LOGO_PREFIX):] if url.startswith(LOGO_PREFIX) else url


def _host(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", url)
    return m.group(1).lower() if m else None


def _flags(up_to: bool, was=None, mn=None):
    f = {}
    if up_to:
        f["u"] = 1
    if was is not None:
        f["w"] = was
    if mn is not None:
        f["m"] = mn
    return f or None


def build_rows(rak_stores: list[dict], pairing: dict, hist_r: dict, hist_h: dict, now: int) -> list[dict]:
    rows: list[dict] = []
    used_keys: set[str] = set()
    seen_r: set[str] = set()
    seen_h: set[str] = set()

    def unique_key(base: str) -> str:
        k = base or "store"
        if k in used_keys:
            i = 2
            while f"{k}-{i}" in used_keys:
                i += 1
            k = f"{k}-{i}"
        used_keys.add(k)
        return k

    # Site records are kept lean: store URLs and display text are derived in the browser from
    # slug/label + rate/kind/up_to; Rakuten logos are stored relative to their CDN prefix.
    def rak_record(r: dict) -> dict:
        seen_r.add(r["id"])
        hist_mod.record(hist_r, r["id"], now, r["rate"], r["kind"], _flags(r["up_to"], r.get("was")))
        st = hist_mod.stats(hist_r["stores"][r["id"]], now)
        return _clean({
            "id": r["id"], "slug": r["slug"],
            "rate": r["rate"], "kind": r["kind"], "up_to": r["up_to"],
            "was": r.get("was"), "boosted": r.get("boosted"),
            **st,
        })

    def honey_record(raw: dict) -> dict | None:
        h = honey.normalise(raw)
        if not h:
            return None
        seen_h.add(h["id"])
        flags = _flags(h["up_to"], None, h.get("min"))
        if not hist_h["stores"].get(h["id"]):
            points = []
            if h.get("prev_rate") is not None and h.get("prev_started") and h["prev_started"] < now:
                points.append((h["prev_started"], h["prev_rate"], h["prev_kind"], None))
            start = h["started"] if h.get("started") and h["started"] < now else now
            if points and start <= points[0][0]:
                start = points[0][0] + 1
            points.append((start, h["rate"], h["kind"], flags))
            hist_mod.seed(hist_h, h["id"], points)
        hist_mod.record(hist_h, h["id"], now, h["rate"], h["kind"], flags)
        st = hist_mod.stats(hist_h["stores"][h["id"]], now)
        return _clean({
            "id": h["id"], "label": h["label"], "site": _host(h.get("site")),
            "rate": h["rate"], "kind": h["kind"], "up_to": h["up_to"], "min": h.get("min"),
            "started": h.get("started"), "boosted": h.get("boosted"),
            "offer_prev": h.get("prev_rate"), "coupons": h.get("coupons") or None,
            **st,
        })

    pairs = pairing["pairs"]
    for r in sorted(rak_stores, key=lambda s: s["name"].lower()):
        rec_r = rak_record(r)
        rec_h = honey_record(pairs[r["id"]]) if r["id"] in pairs else None
        rows.append(_clean({
            "key": unique_key(r["slug"] or f"r{r['id']}"),
            "name": r["name"],
            "logo": _logo(r.get("logo")),
            "r": rec_r,
            "h": rec_h,
        }))
    for raw in sorted(pairing["honey_only"], key=lambda s: (s.get("name") or "").lower()):
        rec_h = honey_record(raw)
        if not rec_h:
            continue
        # Honey-only stores earn a row once they have paid a reward at some point; the thousands of
        # coupon-only stores with no rewards history are left out.
        if rec_h.get("rate") is None and rec_h.get("highAll") is None:
            continue
        rows.append(_clean({
            "key": unique_key(rec_h["label"]),
            "name": raw.get("name"),
            "h": rec_h,
        }))

    # stores that vanished from a site: close their open history segment
    for sid, entries in hist_r["stores"].items():
        if sid not in seen_r and entries and entries[-1][1] is not None:
            hist_mod.record(hist_r, sid, now, None, None, None)
    for sid, entries in hist_h["stores"].items():
        if sid not in seen_h and entries and entries[-1][1] is not None:
            hist_mod.record(hist_h, sid, now, None, None, None)

    for row in rows:
        r, h = row.get("r"), row.get("h")
        rr, hr = (r or {}).get("rate"), (h or {}).get("rate")
        rk, hk = (r or {}).get("kind"), (h or {}).get("kind")
        best = None
        if rr is not None and hr is not None:
            if rk == hk:
                best = "r" if rr > hr else "h" if hr > rr else "tie"
            elif rk == "pct" and hk == "pts":
                best = "r"  # percentage beats a small fixed points reward for comparison purposes
            elif rk == "usd" and hk == "pts":
                best = "r" if rr > hr / honey.POINTS_PER_DOLLAR else "h" if hr / honey.POINTS_PER_DOLLAR > rr else "tie"
        elif rr is not None:
            best = "r"
        elif hr is not None:
            best = "h"
        if best:
            row["best"] = best
        row["max_pct"] = max([x for x in (rr if rk == "pct" else None, hr if hk == "pct" else None) if x is not None],
                             default=None)
        if row["max_pct"] is None:
            row.pop("max_pct")
    rows.sort(key=lambda x: x["name"].lower())
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--max-lookups", type=int, default=None)
    args = ap.parse_args(argv)

    t0 = time.time()
    now = now_ts()
    data = args.data_dir
    config = load_json(args.config, {})
    site_url = config.get("site_url") or ""
    title = config.get("site_title") or "Cash-Back Tracker"
    max_lookups = args.max_lookups if args.max_lookups is not None else int((config.get("lookup") or {}).get("max_lookups_per_run", 300))

    prev_latest = load_json(os.path.join(data, "latest.json"), None)
    hist_r = load_json(os.path.join(data, "history", "rakuten.json"), hist_mod.new_history())
    hist_h = load_json(os.path.join(data, "history", "honey.json"), hist_mod.new_history())
    store_map = load_json(os.path.join(data, "store_map.json"), {"v": 1})
    alerts_file = load_json(os.path.join(data, "alerts.json"), {"v": 1, "alerts": []})
    runs = load_json(os.path.join(data, "runs.json"), [])

    try:
        rak_stores = rakuten.fetch_all_stores()
    except FetchError as e:
        log(f"FATAL rakuten: {e}")
        return 2
    try:
        honey_top = honey.fetch_top_stores()
    except FetchError as e:
        log(f"FATAL honey: {e}")
        return 3

    pairing = matching.pair_stores(rak_stores, honey_top, store_map, config, max_lookups)
    rows = build_rows(rak_stores, pairing, hist_r, hist_h, now)

    prev_by_key = {r["key"]: r for r in prev_latest["stores"]} if prev_latest and prev_latest.get("stores") else None
    new_alerts = alerts_mod.detect(rows, prev_by_key, config, now)
    alerts_file["alerts"] = alerts_mod.merge(alerts_file.get("alerts", []), new_alerts)

    honey_active = sum(1 for r in rows if (r.get("h") or {}).get("rate") is not None)
    meta = {
        "version": VERSION,
        "generated_at": iso(now),
        "ts": now,
        "duration_s": round(time.time() - t0, 1),
        "schedule_utc": config.get("schedule_utc") or [],
        "counts": {
            "stores": len(rows),
            "rakuten": len(rak_stores),
            "honey": pairing["stats"].get("honey_total", 0),
            "honey_active": honey_active,
            "paired": pairing["stats"].get("pairs", 0),
            "boosted_rakuten": sum(1 for r in rows if (r.get("r") or {}).get("boosted")),
            "boosted_honey": sum(1 for r in rows if (r.get("h") or {}).get("boosted")),
            "alerts": len(new_alerts),
        },
        "matching": pairing["stats"],
        "first_run": prev_by_key is None,
    }
    save_json(os.path.join(data, "latest.json"), {"meta": meta, "stores": rows}, compact=True)
    save_json(os.path.join(data, "history", "rakuten.json"), hist_r, compact=True)
    save_json(os.path.join(data, "history", "honey.json"), hist_h, compact=True)
    save_json(os.path.join(data, "store_map.json"), store_map, compact=True)
    save_json(os.path.join(data, "alerts.json"), alerts_file, compact=True)
    alerts_mod.write_feed(alerts_file["alerts"], os.path.join(data, "feed.xml"), site_url, title)
    comment_path = os.path.join(data, "alert_comment.md")
    md = alerts_mod.comment_markdown(new_alerts, site_url, now)
    if md:
        with open(comment_path, "w", encoding="utf-8") as f:
            f.write(md)
    elif os.path.exists(comment_path):
        os.remove(comment_path)
    runs.append({"ts": now, "at": iso(now), "duration_s": meta["duration_s"], **meta["counts"]})
    save_json(os.path.join(data, "runs.json"), runs[-400:], compact=True)

    log(f"done in {meta['duration_s']}s: {meta['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
