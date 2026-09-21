#!/usr/bin/env python3
"""Fetch China A-share index quotes in Actions and evaluate direction with TypeSafe Jev."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "a_share_jev.json"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18&secids=1.000001,0.399001,0.399006"
LABELS = {"up": "上涨", "down": "下跌", "flat": "持平", "insufficient": "数据不足"}

def get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "gdeltorg-a-share-jev/1.0"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

def quote_rows() -> list[dict]:
    payload = get_json(QUOTE_URL)
    rows = []
    for item in payload.get("data", {}).get("diff", []) or []:
        if item.get("f3") is None or item.get("f2") is None:
            continue
        rows.append({"code": str(item.get("f12", "")), "name": item.get("f14", ""), "price": item.get("f2"), "change_percent": item.get("f3"), "change": item.get("f4"), "open": item.get("f17"), "previous_close": item.get("f18"), "high": item.get("f15"), "low": item.get("f16")})
    return rows

def local_judgment(rows: list[dict]) -> dict:
    values = [float(row["change_percent"]) for row in rows if row.get("change_percent") is not None]
    if len(values) < 3:
        choice = "insufficient"
    else:
        average = sum(values) / len(values)
        choice = "up" if average > 0.05 else "down" if average < -0.05 else "flat"
    probs = {key: (1.0 if key == choice else 0.0) for key in LABELS}
    return {"choice": choice, "label": LABELS[choice], "probabilities": probs, "confidence": 0.35, "provider": "deterministic_quote_fallback"}

def jev_judgment(rows: list[dict]) -> dict | None:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        return None
    state = {"market": "China A-share major indices", "as_of": datetime.now(timezone.utc).isoformat(), "indices": rows, "rule": "Judge observed session direction from all three index change_percent values; do not forecast."}
    body = {"state": state, "model": "jev-latest", "questions": {"direction": {"type": "choice", "instructions": "What is the observed direction of today's China A-share major indices based only on the supplied change_percent values? This is not a forecast.", "criteria": {"up": "The broad set of indices is up versus the previous close.", "down": "The broad set of indices is down versus the previous close.", "flat": "The broad set is mixed or near unchanged.", "insufficient": "There is not enough valid quote data."}}}}
    req = Request("https://api.typesafe.ai/v1/systemone", data=json.dumps(body).encode(), headers={"Authorization": key, "Content-Type": "application/json", "User-Agent": "gdeltorg-a-share-jev/1.0"}, method="POST")
    try:
        with urlopen(req, timeout=45) as response:
            answer = json.loads(response.read().decode()).get("answers", {}).get("direction", {})
        choice = answer.get("choice") if answer.get("choice") in LABELS else "insufficient"
        return {"choice": choice, "label": LABELS[choice], "probabilities": answer.get("probabilities", {}), "confidence": answer.get("confidence", 0), "provider": "typesafe_jev", "model": "jev-latest"}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        print(f"TypeSafe Jev HTTP {error.code}: {detail}", flush=True)
        return None
    except (URLError, TimeoutError, ValueError) as error:
        print(f"TypeSafe Jev request failed: {type(error).__name__}: {error}", flush=True)
        return None

def main() -> None:
    rows = quote_rows()
    judgment = jev_judgment(rows) or local_judgment(rows)
    payload = {"schema_version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(), "source": "Eastmoney push2 quote API", "source_url": QUOTE_URL, "market": "中国 A 股主要指数", "indices": rows, "judgment": judgment, "method": "observed quote direction; Jev does not forecast"}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
