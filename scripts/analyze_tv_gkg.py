#!/usr/bin/env python3
"""Build a compact TV-GKG statistics snapshot for the static dashboard."""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://data.gdeltproject.org/gdeltv2_iatelevision/"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "video.json"
UA = "gdeltorg-video-dashboard/1.0"


def get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=90) as response:
        return response.read()


def latest_file() -> tuple[str, str]:
    text = get(BASE + "lastupdate.txt").decode("utf-8", "replace")
    match = re.search(r"(?m)^\s*\d+\s+\S+\s+(https?://\S+\.gkg\.csv\.gz)\s*$", text)
    if not match:
        raise RuntimeError("TV-GKG latest file was not published")
    return match.group(1), match.group(1).rsplit("/", 1)[-1][:8]


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def names(value: str, limit: int = 15) -> list[str]:
    result = []
    for item in value.split(";"):
        name = clean(item.split(",")[0])
        if name and name not in result:
            result.append(name)
    return result[:limit]


def locations(value: str) -> list[str]:
    result = []
    for item in value.split(";"):
        parts = item.split("#")
        if len(parts) >= 3:
            country = clean(parts[2])
            if country and country not in result:
                result.append(country)
    return result


def build() -> dict:
    url, data_day = latest_file()
    rows = gzip.GzipFile(fileobj=io.BytesIO(get(url)))
    records = sources = 0
    source_counts: Counter[str] = Counter()
    themes: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    tone_total = 0.0
    tone_count = 0
    broadcasts = []
    tone_buckets: Counter[str] = Counter()
    reader = csv.reader(io.TextIOWrapper(rows, encoding="utf-8", errors="replace"), delimiter="\t")
    for row in reader:
        if len(row) < 16:
            continue
        records += 1
        source = clean(row[3]) or "unknown"
        source_counts[source] += 1
        themes.update(names(row[8]))
        countries.update(locations(row[10]))
        try:
            tone = float(row[15].split(",")[0])
            tone_total += tone
            tone_count += 1
            tone_buckets["positive" if tone > 2 else "negative" if tone < -2 else "neutral"] += 1
        except (ValueError, IndexError):
            pass
        if len(broadcasts) < 100:
            identifier = clean(row[4])
            title = re.sub(r"^\d{8}_\d{6}_", "", identifier).replace("_", " ").strip() or source
            broadcasts.append({
                "id": identifier,
                "title": title,
                "source": source,
                "date": clean(row[1]),
                "themes": names(row[8], 8),
                "countries": locations(row[10])[:8],
                "tone": round(tone, 3) if tone_count and row[15] else None,
                "replay_url": f"https://archive.org/details/{identifier}" if identifier else "",
            })
    sources = len(source_counts)
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        lag_days = (datetime.now(timezone.utc).date() - datetime.strptime(data_day, "%Y%m%d").date()).days
    except ValueError:
        lag_days = None
    return {
        "schema_version": "1.0",
        "generated_at": generated,
        "source": "https://data.gdeltproject.org/gdeltv2_iatelevision/",
        "source_label": "GDELT TV-GKG / Internet Archive Television News Archive",
        "data_day": data_day,
        "lag_days": lag_days,
        "freshness": "current" if lag_days is not None and lag_days <= 3 else "historical_or_delayed",
        "scope": "美国电视新闻档案；用于全球叙事语境分析，数据有延迟且字幕存在识别噪声。",
        "records": records,
        "sources": sources,
        "average_tone": round(tone_total / tone_count, 3) if tone_count else None,
        "tone_buckets": [{"name": name, "count": count} for name, count in tone_buckets.items()],
        "top_sources": [{"name": name, "count": count} for name, count in source_counts.most_common(15)],
        "top_themes": [{"name": name, "count": count} for name, count in themes.most_common(20)],
        "top_countries": [{"name": name, "count": count} for name, count in countries.most_common(20)],
        "broadcasts": broadcasts,
        "news_items": broadcasts,
        "latest_file": url,
    }


def main() -> None:
    payload = build()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
