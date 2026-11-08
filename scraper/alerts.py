"""Spike detection + outputs (alerts.json, RSS feed, markdown for the GitHub issue comment)."""
from __future__ import annotations

import html
import os
import time

from .common import iso

DAY = 86400
SITE_NAMES = {"r": "Rakuten", "h": "Honey"}


def _fmt_rate(rate, kind, up_to=False, mn=None) -> str:
    if rate is None:
        return "no reward"
    if kind == "pct":
        s = f"{rate:g}%"
    elif kind == "usd":
        s = f"${rate:g}"
    elif kind == "pts":
        s = f"{int(rate):,} pts" if mn is None or mn == rate else f"{int(mn):,}–{int(rate):,} pts"
    else:
        s = f"{rate:g}"
    return ("up to " + s) if up_to and kind != "pts" else s


def detect(rows: list[dict], prev_rows_by_key: dict[str, dict] | None, config: dict, now: int) -> list[dict]:
    """Compare this run against the previous one and return alert events (newest run only)."""
    if prev_rows_by_key is None:
        return []  # first run ever: nothing to compare against
    cfg = config.get("alerts") or {}
    min_rate = float(cfg.get("min_rate_pct", 10))
    min_jump = float(cfg.get("min_increase_points", 5))
    mult = float(cfg.get("boost_multiplier", 2.0))
    high_days = float(cfg.get("new_all_time_high_after_days", 7))
    watch = {w.lower() for w in (cfg.get("watchlist") or [])}
    out: list[dict] = []
    for row in rows:
        prev_row = prev_rows_by_key.get(row["key"]) or {}
        for site in ("r", "h"):
            cur = row.get(site)
            if not cur or cur.get("rate") is None:
                continue
            prev = (prev_row.get(site) or {}).get("rate")
            prev_kind = (prev_row.get(site) or {}).get("kind")
            if prev is not None and prev_kind == cur.get("kind") and cur["rate"] <= prev:
                continue  # no increase
            if prev is not None and prev_kind != cur.get("kind"):
                prev = None  # kind changed ($ -> %): treat as new
            reasons: list[str] = []
            kind = cur.get("kind")
            baseline = cur.get("was") if site == "r" else cur.get("prev_rate")
            if baseline is None or baseline <= 0:
                baseline = cur.get("typical")
            if kind == "pct":
                if cur["rate"] >= min_rate and (prev is None or prev < min_rate):
                    reasons.append(f"crossed {min_rate:g}%")
                if prev is not None and cur["rate"] - prev >= min_jump:
                    reasons.append(f"+{cur['rate'] - prev:g} points since last check")
            if baseline and baseline > 0 and cur["rate"] >= mult * baseline and cur["rate"] > baseline:
                reasons.append(f"{cur['rate'] / baseline:.1f}× the usual {_fmt_rate(baseline, kind)}")
            first_seen = cur.get("first_seen")
            if (cur.get("highAll") is not None and cur["rate"] >= cur["highAll"] and first_seen
                    and now - first_seen >= high_days * DAY and (prev is None or cur["rate"] > prev)
                    and cur.get("changes", 0) >= 2):
                reasons.append("new all-time high")
            if row["key"].lower() in watch or row["name"].lower() in watch:
                reasons.append("on your watchlist")
            if not reasons:
                continue
            url = (f"https://www.rakuten.com/shop/{cur.get('slug', '')}" if site == "r"
                   else f"https://www.joinhoney.com/shop/{cur.get('label', '')}")
            out.append({
                "ts": now,
                "key": row["key"],
                "name": row["name"],
                "site": site,
                "site_name": SITE_NAMES[site],
                "from": prev,
                "to": cur["rate"],
                "kind": kind,
                "from_text": _fmt_rate(prev, kind) if prev is not None else "not offered",
                "to_text": _fmt_rate(cur["rate"], kind, cur.get("up_to"), cur.get("min")),
                "baseline": baseline,
                "reasons": reasons,
                "url": url,
            })
    out.sort(key=lambda a: (-(a["to"] if a["kind"] == "pct" else 0), a["name"].lower()))
    return out


def merge(existing: list[dict], new: list[dict], keep: int = 1000) -> list[dict]:
    return (new + existing)[:keep]


def write_feed(alerts: list[dict], path: str, site_url: str, title: str) -> None:
    items = []
    for a in alerts[:100]:
        headline = f"{a['name']}: {a['site_name']} {a['from_text']} → {a['to_text']}"
        desc = f"{a['site_name']} now pays {a['to_text']} at {a['name']} (was {a['from_text']}). " \
               f"Why it's flagged: {', '.join(a['reasons'])}."
        link = a.get("url") or site_url
        guid = f"{a['key']}-{a['site']}-{a['ts']}"
        items.append(
            "<item>"
            f"<title>{html.escape(headline)}</title>"
            f"<link>{html.escape(link)}</link>"
            f"<guid isPermaLink=\"false\">{html.escape(guid)}</guid>"
            f"<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(a['ts']))}</pubDate>"
            f"<description>{html.escape(desc)}</description>"
            "</item>"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{html.escape(title)} — rate spikes</title>"
        f"<link>{html.escape(site_url)}</link>"
        "<description>Cash-back rate spikes on Rakuten and Honey, checked twice a day.</description>"
        f"<lastBuildDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}</lastBuildDate>"
        + "".join(items) +
        "</channel></rss>\n"
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(rss)


def comment_markdown(alerts: list[dict], site_url: str, now: int) -> str:
    if not alerts:
        return ""
    lines = [f"### {len(alerts)} rate spike{'s' if len(alerts) != 1 else ''} — {iso(now)}", "",
             "| Store | Site | Was | Now | Why |", "|---|---|---|---|---|"]
    for a in alerts[:60]:
        lines.append(f"| [{a['name']}]({site_url}#store={a['key']}) | [{a['site_name']}]({a['url']}) | "
                     f"{a['from_text']} | **{a['to_text']}** | {'; '.join(a['reasons'])} |")
    if len(alerts) > 60:
        lines.append(f"\n…and {len(alerts) - 60} more on the site.")
    lines.append(f"\n[Open the tracker]({site_url}) · [RSS feed]({site_url}data/feed.xml)")
    return "\n".join(lines) + "\n"
