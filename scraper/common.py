"""Shared helpers: polite HTTP, JSON I/O, name normalisation, time."""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

# The tracker identifies itself plainly. Both sites serve their public data to this UA.
USER_AGENT = (
    "RakutenHoneyTracker/1.0 (+https://github.com/Mt5-byte/rakuten-honey-tracker; "
    "personal cash-back rate tracker, checks twice a day)"
)


class FetchError(Exception):
    pass


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def fetch(url: str, headers: dict | None = None, timeout: int = 40, retries: int = 4,
          backoff: float = 1.5) -> bytes:
    """GET a URL with gzip support and retries on 429/5xx/network errors."""
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    data = gzip.decompress(data)
                elif enc == "deflate":
                    data = zlib.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            # 4xx other than 429 are not going to get better by retrying
            if e.code == 429 or e.code >= 500:
                last_err = e
            else:
                raise FetchError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
        sleep = backoff * (2 ** attempt)
        log(f"retry {attempt + 1}/{retries} in {sleep:.1f}s for {url[:120]} ({last_err})")
        time.sleep(sleep)
    raise FetchError(f"giving up on {url}: {last_err}")


def fetch_json(url: str, headers: dict | None = None, **kw) -> dict:
    data = fetch(url, headers=headers, **kw)
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise FetchError(f"non-JSON response from {url[:120]}: {data[:120]!r}") from e


def now_ts() -> int:
    return int(time.time())


def iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, obj, compact: bool = False) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


_STOPWORDS = {"the", "inc", "llc", "us", "usa", "official", "store", "shop", "online", "co"}


def norm_name(s: str | None) -> str:
    """Normalise a merchant name/slug for matching: ascii, lowercase, no TLDs or filler words."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\.(com|net|org|co|us|ca|uk|co\.uk|io|tv)\b", "", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _STOPWORDS]
    return "".join(tokens)


def name_tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\.(com|net|org|co|us|ca|uk|io|tv)\b", "", s)
    s = s.replace("&", " and ")
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _STOPWORDS}


def similar(a: str | None, b: str | None) -> bool:
    """Loose check that two merchant names refer to the same store."""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 5 and len(nb) >= 5 and (na.startswith(nb) or nb.startswith(na)):
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    if ta and tb:
        j = len(ta & tb) / len(ta | tb)
        if j >= 0.6:
            return True
    return False


def slug_of_url(url: str | None) -> str:
    if not url:
        return ""
    path = urllib.parse.urlparse(url).path
    return path.rstrip("/").split("/")[-1].lower()
