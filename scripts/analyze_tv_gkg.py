#!/usr/bin/env python3
"""Build a compact TV-GKG statistics snapshot for the static dashboard."""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
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


def jev_score(item: dict) -> tuple[int, str, list[str]]:
    score = 35
    reasons = []
    if item.get("replay_url"):
        score += 20
        reasons.append("有 Archive 回放")
    theme_count = len(item.get("themes", []))
    score += min(20, theme_count * 2)
    if theme_count >= 6:
        reasons.append("主题密度高")
    country_count = len(item.get("countries", []))
    score += min(10, country_count * 2)
    if country_count >= 3:
        reasons.append("地点覆盖广")
    tone = item.get("tone")
    if tone is not None and abs(tone) >= 3:
        score += 10
        reasons.append("Tone 信号显著")
    if item.get("source") in {"CNN", "DW", "RT", "ALJAZ", "CSPAN"}:
        score += 5
        reasons.append("主要电视来源")
    score = min(100, score)
    label = "值得看" if score >= 75 else "可选" if score >= 55 else "低优先级"
    return score, label, reasons


def jev_api_score(item: dict, api_key: str) -> tuple[int, str, list[str], float | None]:
    state = (
        f"Broadcast: {item['title']}; source: {item['source']}; UTC: {item['date']}; "
        f"TV-GKG themes: {', '.join(item['themes']) or 'none'}; "
        f"locations: {', '.join(item['countries']) or 'none'}; Tone: {item['tone']}; "
        f"Archive replay available: {'yes' if item['replay_url'] else 'no'}."
    )
    body = {
        "state": state,
        "model": "jev-latest",
        "questions": {
            "worth_watching": {
                "type": "choice",
                "instructions": "Is this broadcast worth prioritizing for a human news researcher to watch?",
                "criteria": {
                    "worth_watching": "Prioritize watching: strong evidence, broad relevance, or a meaningful event signal.",
                    "optional": "Potentially useful, but not enough signal to prioritize.",
                    "low_priority": "Do not prioritize based on the available metadata.",
                },
            }
        },
    }
    request = Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": api_key, "Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        answer = json.loads(response.read().decode("utf-8"))["answers"]["worth_watching"]
    choice = answer.get("choice", "low_priority")
    probabilities = answer.get("probabilities") or {}
    score = round(float(probabilities.get("worth_watching", 0)) * 100)
    label = {"worth_watching": "值得看", "optional": "可选", "low_priority": "低优先级"}.get(choice, "未评估")
    confidence = answer.get("confidence")
    return score, label, [f"Jev 判断：{label}"], confidence


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
    jev_provider = "local_explainable_fallback"
    jev_key = os.environ.get("TYPESAFE_API_KEY") if os.environ.get("JEV_LIVE_EVAL") == "1" else None
    reader = csv.reader(io.TextIOWrapper(rows, encoding="utf-8", errors="replace"), delimiter="\t")
    for row in reader:
        if len(row) < 16:
            continue
        records += 1
        source = clean(row[3]) or "unknown"
        source_counts[source] += 1
        themes.update(names(row[8]))
        countries.update(locations(row[10]))
        tone = None
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
            item = {
                "id": identifier,
                "title": title,
                "source": source,
                "date": clean(row[1]),
                "themes": names(row[8], 8),
                "countries": locations(row[10])[:8],
                "tone": round(tone, 3) if tone_count and row[15] else None,
                "replay_url": f"https://archive.org/details/{identifier}" if identifier else "",
            }
            if jev_key:
                try:
                    item["jev_score"], item["jev_label"], item["jev_reasons"], item["jev_confidence"] = jev_api_score(item, jev_key)
                    jev_provider = "typesafe_jev"
                except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError):
                    item["jev_score"], item["jev_label"], item["jev_reasons"] = jev_score(item)
            else:
                item["jev_score"], item["jev_label"], item["jev_reasons"] = jev_score(item)
            broadcasts.append(item)
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
        "latest_available_note": "当前官方 TV-GKG 文件的最新可取得日期；不代表今天已完成电视处理。",
        "jev_provider": jev_provider,
        "jev_source": "https://typesafe.ai/blog/introducing-system-one-models-and-jev",
    }


def main() -> None:
    payload = build()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
