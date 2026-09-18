#!/usr/bin/env python3
"""Create the live GDELT snapshot and append a bounded historical archive.

GDELT's public 2.1 endpoint exposes rolling files, not an unlimited history API.
Therefore history begins at the first successful run and is retained in git.
"""
from __future__ import annotations

import html
import io
import json
import re
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE = "https://data.gdeltproject.org/gdeltv2/"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVES = DATA / "archives"
UA = "gdeltorg-signal-feed/1.0"
MAX_ROWS = 100_000


def get(url: str, timeout: int = 90) -> bytes:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as response:
        return response.read()


def latest_files() -> dict[str, str]:
    text = get(BASE + "lastupdate.txt").decode("utf-8", "replace")
    found: dict[str, str] = {}
    for url in re.findall(r"https?://[^\s]+", text):
        url = url.rstrip(".,")
        lower = url.lower()
        if lower.endswith(".export.csv.zip"): found["events"] = url
        elif lower.endswith(".gkg.csv.zip"): found["gkg"] = url
        elif lower.endswith(".mentions.csv.zip"): found["mentions"] = url
    missing = {"events", "gkg", "mentions"} - found.keys()
    if missing: raise RuntimeError(f"Missing datasets: {sorted(missing)}")
    return found


def rows(url: str):
    with zipfile.ZipFile(io.BytesIO(get(url))) as archive:
        with archive.open(archive.namelist()[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            for line in stream:
                yield line.rstrip("\n").split("\t")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def domain(url: str) -> str:
    try: return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError: return ""


def locations(value: str) -> list[dict[str, str]]:
    result, seen = [], set()
    for item in value.split(";"):
        parts = item.split("#")
        if len(parts) < 3: continue
        name, country = clean(parts[1]), clean(parts[2])
        if name and country and (name, country) not in seen:
            result.append({"name": name, "country": country})
            seen.add((name, country))
    return result[:12]


def page_meta(url: str) -> tuple[str, str]:
    if not url.startswith(("http://", "https://")): return "", ""
    try:
        source = get(url, 10)[:600_000].decode("utf-8", "replace")
        def find(*names: str) -> str:
            names_re = "|".join(re.escape(name) for name in names)
            patterns = [
                rf'<meta[^>]+(?:property|name)=["\'](?:{names_re})["\'][^>]+content=["\']([^"\']*)',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\'](?:{names_re})["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, source, re.I)
                if match: return clean(match.group(1))
            return ""
        title = find("og:title", "twitter:title")
        summary = find("og:description", "twitter:description", "description")
        if not title:
            match = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
            title = clean(match.group(1)) if match else ""
        return title, summary
    except Exception:
        return "", ""


def gkg_events(url: str) -> list[dict]:
    ranked = []
    for index, row in enumerate(rows(url)):
        if index >= MAX_ROWS or len(row) < 10: break
        article = clean(row[3]); source_name = clean(row[2]) or domain(article)
        themes = [clean(x.split(",")[0].replace("_", " ")) for x in (row[6] or "").split(";") if x.strip()]
        places = locations(row[7] if len(row) > 7 else "")
        persons, orgs = clean(row[8] if len(row) > 8 else ""), clean(row[9] if len(row) > 9 else "")
        score = len(themes) * 3 + len(places) * 5 + bool(persons) * 4 + bool(orgs) * 4 + min(len(source_name), 50)
        ranked.append({"id": article or f"gkg-{index}", "url": article, "source_name": source_name,
                       "date": row[0][:12], "score": score, "themes": themes[:20],
                       "locations": places, "entities": {"persons": persons[:500], "organizations": orgs[:500]}})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    for item in ranked[:40]:
        title, summary = page_meta(item["url"])
        item["title"] = title or item["source_name"] or "未命名报道"
        item["summary"] = summary or "该信号来自 GDELT 公共新闻数据，点击原文查看完整报道。"
        time.sleep(.04)
    return ranked[:40]


def event_rows(url: str) -> list[dict]:
    result = []
    for index, row in enumerate(rows(url)):
        if index >= MAX_ROWS or len(row) < 6: break
        date = row[1] if len(row) > 1 else row[0]
        result.append({"id": row[0] if row else f"event-{index}", "date": date, "event_code": row[26] if len(row) > 26 else "", "quad_class": row[29] if len(row) > 29 else "", "actor1": row[5] if len(row) > 5 else "", "actor2": row[15] if len(row) > 15 else "", "location": row[52] if len(row) > 52 else "", "source_url": row[60] if len(row) > 60 else ""})
    return result


def snapshot(files: dict[str, str], stamp: str) -> dict:
    stories = gkg_events(files["gkg"])
    events = event_rows(files["events"])
    datasets = {}
    for name in ("events", "gkg", "mentions"):
        datasets[name] = {"records": sum(1 for _ in rows(files[name])), "file": files[name].rsplit("/", 1)[-1], "status": "ready"}
    hours = Counter((item["date"] or "")[:10] for item in events)
    themes = Counter(t for item in stories for t in item["themes"])
    countries = Counter(p["country"] for item in stories for p in item["locations"])
    timeline = [{"date": key, "events": value} for key, value in sorted(hours.items())]
    return {"schema_version": "4.0", "generated_at": stamp, "data_time": stamp, "source": BASE,
            "datasets": datasets, "events": events[:5000], "top_news": stories,
            "timeline": timeline, "facets": {"themes": [{"name": k, "count": v} for k, v in themes.most_common(80)], "countries": [{"name": k, "count": v} for k, v in countries.most_common()]}}


def main() -> None:
    files = latest_files(); stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = snapshot(files, stamp); DATA.mkdir(exist_ok=True); ARCHIVES.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_id = stamp.replace(":", "-"); archive_file = ARCHIVES / f"{archive_id}.json"
    archive_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_file = DATA / "history.json"
    history = json.loads(history_file.read_text(encoding="utf-8")) if history_file.exists() else {"schema_version": "4.0", "started_at": stamp, "snapshots": []}
    history["started_at"] = history.get("started_at") or stamp
    history["snapshots"] = [x for x in history.get("snapshots", []) if x.get("id") != archive_id]
    history["snapshots"].append({"id": archive_id, "generated_at": stamp, "file": f"archives/{archive_file.name}", "datasets": {n: payload["datasets"][n]["records"] for n in ("events", "gkg", "mentions")}, "timeline_points": len(payload["timeline"])})
    history["snapshots"] = history["snapshots"][-10000:]
    history["totals"] = {n: sum(x["datasets"].get(n, 0) for x in history["snapshots"]) for n in ("events", "gkg", "mentions")}
    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__": main()
