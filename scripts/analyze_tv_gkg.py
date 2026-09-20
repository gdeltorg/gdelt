#!/usr/bin/env python3
"""Build a compact TV-GKG statistics snapshot for the static dashboard."""
from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import gzip
import html
import io
import json
import os
import re
from urllib.parse import quote_plus
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://data.gdeltproject.org/gdeltv2_iatelevision/"
ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "video.json"
UA = "gdeltorg-video-dashboard/1.0"


def get(url: str, timeout: int = 90) -> bytes:
    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_archive_videos(limit: int = 100) -> list[dict]:
    params = (
        "?q=collection%3Atvnews&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=date"
        f"&sort%5B%5D=date+desc&rows={limit}&output=json"
    )
    payload = json.loads(get(ARCHIVE_SEARCH + params).decode("utf-8"))
    result = []
    docs = payload.get("response", {}).get("docs", [])
    identifiers = [clean(doc.get("identifier", "")) for doc in docs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        snippets = list(pool.map(lambda value: archive_snippet(f"https://archive.org/details/{value}") if value else "", identifiers))
    for doc, transcript in zip(docs, snippets):
        identifier = clean(doc.get("identifier", ""))
        if not identifier:
            continue
        source = identifier.split("_", 1)[0]
        title = clean(doc.get("title", "")) or identifier.replace("_", " ")
        date = clean(doc.get("date", ""))
        program = title.split(" : ")[0].strip() if " : " in title else title
        human_title = f"{source}《{program}》电视节目（{date[:10] or '日期未知'}）"
        page_url = f"https://archive.org/details/{identifier}"
        raw_transcript = transcript
        transcript = usable_transcript(transcript)
        if transcript:
            human_title = transcript.split(". ", 1)[0].strip()[:180]
        summary = " ".join(transcript.split()[:70]) + ("…" if len(transcript.split()) > 70 else "") if transcript else (
            "节目级摘要："
            f"{source} 的《{program}》于 {date or '未知时间'} 进入电视档案。"
            "公开页面暂未取得可读字幕正文，需打开原片核验。"
        )
        result.append({
            "id": identifier,
            "title": title,
            "title_candidate": human_title,
            "human_title": human_title,
            "summary": summary,
            "human_summary": summary,
            "model_analysis": {
                "provider": "not_configured",
                "judgment": "可优先核验" if transcript else "信息不足，暂不判定新闻价值",
                "confidence": 0.35 if transcript else 0.0,
                "basis": "依据 Archive 页面字幕/ASR 摘要和关键词，仍需打开原片核验。" if transcript else "当前只有 Archive 目录元数据，没有公开 ASR/字幕正文。",
            },
            "source": source,
            "source_url": f"https://archive.org/details/{identifier}",
            "date": date,
            "keywords": extract_keywords(transcript),
            "related_links": gdelt_links(extract_keywords(transcript)),
            "themes": [],
            "countries": [],
            "extraction_methods": ["Internet Archive tvnews 目录"] + (["Archive 页面字幕/ASR 摘要"] if transcript else []),
            "text_sources": [page_url] if transcript else [],
            "extraction_status": {
                "caption": "available" if transcript else "catalog_only",
                "asr": "available" if transcript else "not_in_catalog",
                "ocr": "not_in_catalog",
                "lip_reading": "not_supported",
            },
            "replay_url": f"https://archive.org/details/{identifier}",
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "transcript_text": transcript[:12000],
            "transcript_text_raw": raw_transcript[:12000],
            "transcript_source": page_url if transcript else "",
        })
    return result


def archive_snippet(url: str) -> str:
    try:
        page = get(url, timeout=20).decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError):
        return ""
    match = re.search(r'<div class="snippet">\s*<div class="snipin[^>]*>(.*?)</div>', page, re.S)
    if not match:
        return ""
    return clean(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))))


def usable_transcript(text: str) -> str:
    """Reject audio-stage directions that cannot support a news summary."""
    cleaned = clean(text)
    cleaned = re.sub(r"\[[^\]]{1,120}\]", " ", cleaned)
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]{2,}", cleaned)
    return cleaned if len(words) >= 8 else ""


def gdelt_links(keywords: list[str]) -> list[dict[str, str]]:
    if not keywords:
        return []
    query = " ".join(keywords[:5])
    encoded = quote_plus(query)
    return [
        {
            "label": "GDELT TV 片段",
            "url": f"https://api.gdeltproject.org/api/v2/tv/tv?format=html&mode=clipgallery&query={encoded}",
        },
        {
            "label": "GDELT 新闻/GKG",
            "url": f"https://api.gdeltproject.org/api/v2/doc/doc?format=html&mode=artlist&query={encoded}",
        },
        {
            "label": "GDELT 事件",
            "url": f"https://api.gdeltproject.org/api/v2/events/events?format=html&query={encoded}",
        },
    ]


def extract_keywords(text: str, limit: int = 12) -> list[str]:
    if not text:
        return []
    stop = {"the", "and", "that", "this", "with", "from", "have", "they", "were", "about", "your", "what", "will", "you"}
    words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text.lower())
    return [word for word, _ in Counter(word for word in words if word not in stop).most_common(limit)]


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
        f"Archive replay available: {'yes' if item['replay_url'] else 'no'}; "
        f"transcript excerpt: {item.get('transcript_text', '')[:5000]}"
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


def metadata_headline(source: str, themes: list[str], countries: list[str]) -> str:
    signals = themes[:2] + countries[:1]
    return f"{source} 电视广播：{'、'.join(signals)}" if signals else f"{source} 电视广播主题候选"


def metadata_summary(source: str, themes: list[str], countries: list[str], tone: float | None) -> str:
    parts = [f"该条 {source} 广播记录由 TV-GKG 字幕知识图谱提取"]
    if themes:
        parts.append(f"主题信号包括 {'、'.join(themes[:5])}")
    if countries:
        parts.append(f"涉及地点字段包括 {'、'.join(countries[:3])}")
    if tone is not None:
        parts.append(f"Tone 为 {tone:.2f}")
    return "；".join(parts) + "。这是一条机器生成摘要，需打开原片核验。"


def build() -> dict:
    url, data_day = latest_file()
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    jev_key = os.environ.get("TYPESAFE_API_KEY") if os.environ.get("JEV_LIVE_EVAL") == "1" else None
    latest_videos = latest_archive_videos()
    if jev_key:
        for item in latest_videos:
            try:
                score, label, reasons, confidence = jev_api_score(item, jev_key)
                item["jev_score"] = score
                item["jev_label"] = label
                item["jev_reasons"] = reasons
                item["jev_confidence"] = confidence
                item["model_analysis"] = {
                    "provider": "typesafe_jev",
                    "judgment": label,
                    "confidence": confidence,
                    "basis": "Archive 目录元数据；未包含公开 ASR/字幕正文。",
                }
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError):
                pass
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
            item_themes = names(row[8], 8)
            item_countries = locations(row[10])[:8]
            item = {
                "id": identifier,
                "title": title,
                "title_candidate": metadata_headline(source, item_themes, item_countries),
                "summary": metadata_summary(source, item_themes, item_countries, tone),
                "source": source,
                "source_url": f"https://archive.org/details/{identifier}" if identifier else "",
                "date": clean(row[1]),
                "themes": item_themes,
                "countries": item_countries,
                "keywords": item_themes + [country for country in item_countries if country not in item_themes],
                "extraction_methods": ["TV-GKG 字幕主题"],
                "extraction_status": {
                    "caption": "available_metadata",
                    "asr": "not_in_snapshot",
                    "ocr": "not_in_snapshot",
                    "lip_reading": "not_supported",
                },
                "tone": round(tone, 3) if tone_count and row[15] else None,
                "replay_url": f"https://archive.org/details/{identifier}" if identifier else "",
                "generated_at": generated,
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
        "latest_videos": latest_videos,
        "latest_video_source": "https://archive.org/advancedsearch.php?q=collection%3Atvnews&sort%5B%5D=date+desc",
        "latest_video_day": latest_videos[0]["date"][:10] if latest_videos else None,
        "latest_file": url,
        "latest_available_note": "当前官方 TV-GKG 文件的最新可取得日期；不代表今天已完成电视处理。",
        "extraction_catalog": {
            "caption": "TV-GKG 字幕主题与节目元数据",
            "asr": "TVAI ASR 或用户导入字幕后可用",
            "ocr": "TVAI OCR 或本地/后端视频引擎后可用",
            "lip_reading": "当前未接入；不能从 TV-GKG 推断口型内容",
        },
        "jev_provider": jev_provider,
        "jev_source": "https://typesafe.ai/blog/introducing-system-one-models-and-jev",
    }


def main() -> None:
    payload = build()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
