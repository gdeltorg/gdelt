#!/usr/bin/env python3
"""Build a free, local visual-search index with Chinese-CLIP.

The workflow downloads the public model weights in GitHub Actions. No API key
or external inference service is required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import torch
from PIL import Image
from transformers import ChineseCLIPModel, ChineseCLIPProcessor

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "video.json"
OUTPUT = ROOT / "data" / "visual_index.json"
MODEL = "OFA-Sys/chinese-clip-vit-base-patch16"
UA = "gdeltorg-visual-index/1.0"
LIMIT = max(1, int(os.environ.get("VISUAL_INDEX_LIMIT", "60")))
THRESHOLD = float(os.environ.get("VISUAL_INDEX_THRESHOLD", "0.18"))

# These phrases make the first index useful before a captioning service exists.
# The same embedding space supports later additions without changing the index.
LABELS = [
    "马", "树林", "奔跑", "动物", "人", "人群", "汽车", "卡车", "火车", "飞机",
    "船", "道路", "城市街道", "乡村", "森林", "海滩", "山", "河流", "建筑物",
    "火灾", "烟雾", "爆炸", "洪水", "抗议活动", "警察", "军队", "演讲",
    "新闻演播室", "体育比赛", "足球", "篮球", "旗帜", "地图", "屏幕文字",
    "a horse", "a forest", "running", "an animal", "a crowd", "a car",
    "a truck", "a train", "an airplane", "a ship", "a road", "a city street",
    "the countryside", "a beach", "a mountain", "a river", "a building",
    "fire", "smoke", "an explosion", "flooding", "a protest", "police",
    "military", "a speech", "a news studio", "a sports match", "soccer",
    "basketball", "a flag", "a map", "text on screen",
]


def fetch_image(url: str) -> Image.Image:
    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=45) as response:
        return Image.open(response).convert("RGB")


def normalized(features: torch.Tensor) -> torch.Tensor:
    if not isinstance(features, torch.Tensor):
        features = features.pooler_output
    return features / features.norm(p=2, dim=-1, keepdim=True)


def main() -> None:
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    records = data.get("latest_videos", [])[:LIMIT]
    processor = ChineseCLIPProcessor.from_pretrained(MODEL)
    model = ChineseCLIPModel.from_pretrained(MODEL).eval()
    with torch.inference_mode():
        text_inputs = processor(text=LABELS, padding=True, return_tensors="pt")
        label_vectors = normalized(model.get_text_features(**text_inputs))

    entries = []
    for number, item in enumerate(records, start=1):
        image_url = item.get("image_url")
        if not image_url:
            continue
        try:
            image = fetch_image(image_url)
            with torch.inference_mode():
                inputs = processor(images=image, return_tensors="pt")
                image_vector = normalized(model.get_image_features(**inputs))
                scores = (image_vector @ label_vectors.T)[0]
            matches = [
                (LABELS[index], round(float(score), 4))
                for index, score in enumerate(scores)
                if float(score) >= THRESHOLD
            ]
            matches.sort(key=lambda pair: pair[1], reverse=True)
            terms = [label for label, _ in matches[:20]]
            entries.append({
                "id": item.get("id"),
                "title": item.get("human_title") or item.get("title_candidate") or item.get("title"),
                "source": item.get("source"),
                "date": item.get("date"),
                "image_url": image_url,
                "replay_url": item.get("replay_url") or item.get("source_url"),
                "description_zh": "画面候选标签：" + "、".join(terms[:12]),
                "description_en": "Visual candidate labels: " + ", ".join(terms[:12]),
                "keywords": ",".join(terms),
                "visual_labels": [{"label": label, "score": score} for label, score in matches[:20]],
            })
            print(f"[{number}/{len(records)}] indexed {item.get('id')}", flush=True)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"[{number}/{len(records)}] skipped {item.get('id')}: {error}", flush=True)
    OUTPUT.write_text(json.dumps({
        "schema_version": "1.0",
        "generated_at": data.get("generated_at"),
        "model": MODEL,
        "records": len(entries),
        "items": entries,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} ({len(entries)} records)")


if __name__ == "__main__":
    main()
