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
ARCHIVE = DATA / "archive"
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
    missing = {"events", "mentions"} - found.keys()
    if missing:
        raise RuntimeError(f"Missing required datasets: {sorted(missing)}")
    for name in list(found):
        try:
            get(found[name], timeout=15)
        except Exception:
            found.pop(name, None)
    if "gkg" not in found:
        raise RuntimeError("GKG dataset is temporarily unavailable; no snapshot was generated.")
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
        # GKG 2.1: DATE, source common name, document URL, V2Themes,
        # V2Locations, V2Persons and V2Organizations occupy columns 1, 3-10.
        article = clean(row[4] if len(row) > 4 else "")
        source_name = clean(row[3] if len(row) > 3 else "") or domain(article)
        themes = [clean(x.split(",")[0].replace("_", " ")) for x in (row[7] or "").split(";") if x.strip()]
        places = locations(row[8] if len(row) > 8 else "")
        persons, orgs = clean(row[9] if len(row) > 9 else ""), clean(row[10] if len(row) > 10 else "")
        score = len(themes) * 3 + len(places) * 5 + bool(persons) * 4 + bool(orgs) * 4 + min(len(source_name), 50)
        ranked.append({"id": article or f"gkg-{index}", "url": article, "source_name": source_name,
                       "date": (row[1] if len(row) > 1 else row[0])[:12], "score": score, "themes": themes[:20],
                       "locations": places, "entities": {"persons": persons[:500], "organizations": orgs[:500]}})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    for item in ranked[:40]:
        title, summary = page_meta(item["url"])
        item["title"] = title or item["source_name"] or "未命名报道"
        item["summary"] = summary or "该信号来自 GDELT 公共新闻数据，点击原文查看完整报道。"
        item["source"] = item["source_name"] or domain(item["url"])
        time.sleep(.04)
    return ranked[:40]


def event_rows(url: str) -> list[dict]:
    result = []
    for index, row in enumerate(rows(url)):
        if index >= MAX_ROWS or len(row) < 6: break
        date = row[1] if len(row) > 1 else row[0]
        result.append({"id": row[0] if row else f"event-{index}", "date": date, "event_code": row[26] if len(row) > 26 else "", "quad_class": row[29] if len(row) > 29 else "", "actor1": row[5] if len(row) > 5 else "", "actor2": row[15] if len(row) > 15 else "", "location": row[52] if len(row) > 52 else "", "source_url": row[60] if len(row) > 60 else ""})
    return result


def dataset_stats(url: str, date_index: int) -> dict:
    records = 0
    dates = Counter()
    for row in rows(url):
        records += 1
        if len(row) > date_index:
            value = clean(row[date_index])[:8]
            if re.fullmatch(r"\d{8}", value):
                dates[value] += 1
    ordered = sorted(dates)
    return {
        "records": records,
        "time_points": len(ordered),
        "date_start": ordered[0] if ordered else "",
        "date_end": ordered[-1] if ordered else "",
        "date_counts": dict(dates),
    }


def snapshot(files: dict[str, str], stamp: str) -> dict:
    stories = gkg_events(files["gkg"])
    events = event_rows(files["events"])
    datasets = {
        "events": dataset_stats(files["events"], 1),
        "gkg": dataset_stats(files["gkg"], 1),
        "mentions": dataset_stats(files["mentions"], 0),
    }
    for name in datasets:
        datasets[name].update({"file": files[name].rsplit("/", 1)[-1], "status": "ready"})
    hours = Counter()
    for name in ("events", "gkg", "mentions"):
        for key, value in datasets[name]["date_counts"].items():
            if re.fullmatch(r"\d{8}", key):
                hours[f"{key[:4]}-{key[4:6]}-{key[6:]}"] += value
        datasets[name].pop("date_counts", None)
    themes = Counter(t for item in stories for t in item["themes"])
    countries = Counter(p["country"] for item in stories for p in item["locations"])
    timeline = [{"date": key, "events": value} for key, value in sorted(hours.items())]
    return {"schema_version": "4.0", "generated_at": stamp, "data_time": stamp, "source": BASE,
            "datasets": datasets, "events": events[:5000], "top_news": stories,
            "timeline": timeline, "facets": {"themes": [{"name": k, "count": v} for k, v in themes.most_common(80)], "countries": [{"name": k, "count": v} for k, v in countries.most_common()]}}


def main() -> None:
    files = latest_files(); stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = snapshot(files, stamp); DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_id = stamp.replace(":", "-"); archive_file = ARCHIVE / f"{archive_id}.json"
    archive_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_file = DATA / "history.json"
    history = json.loads(history_file.read_text(encoding="utf-8")) if history_file.exists() else {"schema_version": "4.0", "started_at": stamp, "snapshots": []}
    history["started_at"] = history.get("started_at") or stamp
    history["snapshots"] = [x for x in history.get("snapshots", []) if x.get("id") != archive_id]
    history["snapshots"].append({
        "id": archive_id,
        "generated_at": stamp,
        "file": f"archive/{archive_file.name}",
        "datasets": {
            n: {
                "records": payload["datasets"][n]["records"],
                "time_points": payload["datasets"][n]["time_points"],
                "date_start": payload["datasets"][n]["date_start"],
                "date_end": payload["datasets"][n]["date_end"],
            } for n in ("events", "gkg", "mentions")
        },
        "timeline_points": len(payload["timeline"]),
    })
    history["snapshots"] = history["snapshots"][-10000:]
    history["totals"] = {
        n: sum(
            (x.get("datasets", {}).get(n, {}).get("records", 0)
             if isinstance(x.get("datasets", {}).get(n), dict)
             else x.get("datasets", {}).get(n, 0))
            for x in history["snapshots"]
        )
        for n in ("events", "gkg", "mentions")
    }
    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__": main()
