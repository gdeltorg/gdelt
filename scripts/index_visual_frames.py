#!/usr/bin/env python3
"""Build a static visual-search index from the current TV thumbnail records.

The API key is only used in GitHub Actions. The generated index contains
descriptions and source metadata, never credentials or raw image bytes.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "video.json"
OUTPUT = ROOT / "data" / "visual_index.json"
API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
UA = "gdeltorg-visual-index/1.0"
LIMIT = max(1, int(os.environ.get("VISUAL_INDEX_LIMIT", "120")))


def fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=45) as response:
        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return response.read(), content_type if content_type in {"image/jpeg", "image/png", "image/webp"} else "image/jpeg"


def describe(image: bytes, mime_type: str, api_key: str) -> dict:
    prompt = (
        "Analyze this video frame for visual search. Return JSON only with exactly these "
        "string fields: description_zh, description_en, keywords. Describe visible objects, "
        "actions, setting, text, and notable people or vehicles. Do not infer audio or facts "
        "not visible. In description_zh, use natural Chinese and explicitly describe motion "
        "when visible, such as '一匹马在树林里奔跑'. keywords is comma-separated Chinese "
        "and English search terms."
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image).decode("ascii")}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    request = Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urlopen(request, timeout=90) as response:
        body = json.loads(response.read())
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)
    return {
        "description_zh": str(result.get("description_zh", "")).strip(),
        "description_en": str(result.get("description_en", "")).strip(),
        "keywords": str(result.get("keywords", "")).strip(),
    }


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required; configure it as an Actions secret.")
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    records = data.get("latest_videos", [])[:LIMIT]
    entries = []
    for number, item in enumerate(records, start=1):
        image_url = item.get("image_url")
        if not image_url:
            continue
        try:
            image, mime_type = fetch(image_url)
            visual = describe(image, mime_type, api_key)
            entries.append({
                "id": item.get("id"),
                "title": item.get("human_title") or item.get("title_candidate") or item.get("title"),
                "source": item.get("source"),
                "date": item.get("date"),
                "image_url": image_url,
                "replay_url": item.get("replay_url") or item.get("source_url"),
                **visual,
            })
            print(f"[{number}/{len(records)}] indexed {item.get('id')}", flush=True)
        except (HTTPError, URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
            print(f"[{number}/{len(records)}] skipped {item.get('id')}: {error}", flush=True)
        time.sleep(0.2)
    OUTPUT.write_text(json.dumps({
        "schema_version": "1.0",
        "generated_at": data.get("generated_at"),
        "model": "gemini-2.5-flash",
        "records": len(entries),
        "items": entries,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(entries)} records)")


if __name__ == "__main__":
    main()
