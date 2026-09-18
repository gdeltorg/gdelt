#!/usr/bin/env python3
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen

BASE = 'https://data.gdeltproject.org/gdeltv2/'


def download(url):
    with urlopen(url, timeout=90) as resp:
        return resp.read()


def discover_latest_files():
    text = download(BASE + 'lastupdate.txt').decode('utf-8', 'replace')
    files = {}
    for url in re.findall(r'https?://[^\s]+', text):
        low = url.lower()
        if low.endswith('.export.csv.zip'):
            files['events'] = url
        elif low.endswith('.gkg.csv.zip'):
            files['gkg'] = url
        elif low.endswith('.mentions.csv.zip'):
            files['mentions'] = url
    missing = {'events', 'gkg', 'mentions'} - set(files)
    if missing:
        raise RuntimeError(f'Missing GDELT datasets: {sorted(missing)}')
    return files


def iter_rows(url):
    archive = download(url)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as raw:
            text_stream = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
            for line in text_stream:
                yield line.rstrip('\n').split('\t')


def count_records(url):
    return sum(1 for _ in iter_rows(url))


def clean_theme(value):
    value = value.replace('_', ' ')
    return re.sub(r'\s*\([^)]*\)', '', value).strip()


def host(value):
    return re.sub(r'^https?://', '', value).split('/')[0]


def rank_news(gkg_url):
    ranked = []
    for idx, row in enumerate(iter_rows(gkg_url)):
        if idx >= 50000 or len(row) < 10:
            break
        # GKG 2.1: date, collection, source name, document URL, counts,
        # v2 counts, themes, locations, persons, organisations, tone...
        source_name = row[2].strip() if len(row) > 2 else ''
        document_url = row[3].strip() if len(row) > 3 else ''
        themes_field = row[6] if len(row) > 6 else ''
        locations = row[7] if len(row) > 7 else ''
        persons = row[8] if len(row) > 8 else ''
        orgs = row[9] if len(row) > 9 else ''
        themes = [clean_theme(item.split(',')[0]) for item in themes_field.split(';') if item.strip()]
        entity_count = sum(bool(v) for v in (locations, persons, orgs))
        score = len(themes) * 3 + entity_count * 5 + min(len(source_name), 50)
        ranked.append({
            'url': document_url,
            'title': source_name or host(document_url),
            'source': document_url,
            'source_name': source_name,
            'date': row[0][:12] if row else '',
            'score': score,
            'themes': themes[:12]
        })
    ranked.sort(key=lambda x: x['score'], reverse=True)
    return ranked[:10]


def main():
    files = discover_latest_files()
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    payload = {'generated_at': stamp, 'data_time': stamp, 'source': BASE, 'datasets': {}}
    for name in ('events', 'gkg', 'mentions'):
        payload['datasets'][name] = {
            'records': count_records(files[name]),
            'status': 'ready',
            'file': files[name].rsplit('/', 1)[-1]
        }
    payload['top_news'] = rank_news(files['gkg'])
    with open('data/latest.json', 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
