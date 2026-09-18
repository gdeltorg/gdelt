#!/usr/bin/env python3
import html
import io
import json
import re
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE = 'https://data.gdeltproject.org/gdeltv2/'
USER_AGENT = 'gdeltorg-dashboard/2.0 (+https://github.com/gdeltorg/gdelt)'

def download(url, timeout=90):
    req = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(req, timeout=timeout) as response:
        return response.read()

def discover_latest_files():
    text = download(BASE + 'lastupdate.txt').decode('utf-8', 'replace')
    files = {}
    for url in re.findall(r'https?://[^\s]+', text):
        url = url.rstrip('.,')
        low = url.lower()
        if low.endswith('.export.csv.zip'): files['events'] = url
        elif low.endswith('.gkg.csv.zip'): files['gkg'] = url
        elif low.endswith('.mentions.csv.zip'): files['mentions'] = url
    missing = {'events', 'gkg', 'mentions'} - files.keys()
    if missing: raise RuntimeError(f'Missing GDELT datasets: {sorted(missing)}')
    return files

def iter_rows(url):
    archive = download(url)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        with zf.open(zf.namelist()[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
            for line in stream: yield line.rstrip('\n').split('\t')

def count_records(url): return sum(1 for _ in iter_rows(url))
def clean(value): return re.sub(r'\s+', ' ', html.unescape(value or '')).strip()
def theme(value): return clean(value.replace('_', ' '))
def domain(url):
    try: return urlparse(url).netloc.lower().removeprefix('www.')
    except ValueError: return ''

def locations(value):
    result = []
    for item in value.split(';'):
        parts = item.split('#')
        if len(parts) < 3: continue
        name, country = clean(parts[1]), clean(parts[2])
        if name and country: result.append({'name': name, 'country': country})
    unique = []
    seen = set()
    for item in result:
        key = (item['name'], item['country'])
        if key not in seen: unique.append(item); seen.add(key)
    return unique[:8]

def page_metadata(url):
    result = {'title': '', 'summary': ''}
    if not url.startswith(('http://', 'https://')): return result
    try:
        raw = download(url, 12)[:750000].decode('utf-8', 'replace')
        def meta(*names):
            names = '|'.join(re.escape(x) for x in names)
            patterns = [rf'<meta[^>]+(?:property|name)=["\'](?:{names})["\'][^>]+content=["\']([^"\']*)', rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\'](?:{names})["\']']
            for pattern in patterns:
                match = re.search(pattern, raw, re.I)
                if match: return clean(match.group(1))
            return ''
        result['title'] = meta('og:title', 'twitter:title')
        result['summary'] = meta('og:description', 'twitter:description', 'description')
        if not result['title']:
            match = re.search(r'<title[^>]*>(.*?)</title>', raw, re.I | re.S)
            if match: result['title'] = clean(match.group(1))
    except Exception: pass
    return result

def rank_news(gkg_url):
    ranked = []
    for index, row in enumerate(iter_rows(gkg_url)):
        if index >= 50000 or len(row) < 10: break
        source_name, article_url = clean(row[2]), clean(row[3])
        themes = [theme(x.split(',')[0]) for x in (row[6] if len(row) > 6 else '').split(';') if x.strip()]
        places = locations(row[7] if len(row) > 7 else '')
        persons = clean(row[8] if len(row) > 8 else '')
        orgs = clean(row[9] if len(row) > 9 else '')
        score = len(themes) * 3 + len(places) * 5 + bool(persons) * 4 + bool(orgs) * 4 + min(len(source_name), 50)
        ranked.append({'url': article_url, 'source': article_url, 'source_name': source_name or domain(article_url), 'date': row[0][:12], 'score': score, 'themes': themes[:12], 'locations': places, 'entities': {'persons': persons[:300], 'organizations': orgs[:300]}})
    ranked.sort(key=lambda x: x['score'], reverse=True)
    selected = ranked[:20]
    for item in selected:
        meta = page_metadata(item['url'])
        item['title'] = meta['title'] or item['source_name'] or domain(item['url']) or '未命名报道'
        item['summary'] = meta['summary'] or '该条信号来自 GDELT 公共新闻数据，点击查看原文。'
        time.sleep(.1)
    return selected

def main():
    files = discover_latest_files()
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    payload = {'schema_version': '2.0', 'generated_at': stamp, 'data_time': stamp, 'source': BASE, 'datasets': {}}
    for name in ('events', 'gkg', 'mentions'):
        payload['datasets'][name] = {'records': count_records(files[name]), 'status': 'ready', 'file': files[name].rsplit('/', 1)[-1]}
    payload['top_news'] = rank_news(files['gkg'])
    payload['facets'] = {'countries': [{'name': k, 'count': v} for k, v in Counter(p['country'] for x in payload['top_news'] for p in x['locations']).most_common()], 'themes': [{'name': k, 'count': v} for k, v in Counter(t for x in payload['top_news'] for t in x['themes']).most_common(40)]}
    with open('data/latest.json', 'w', encoding='utf-8') as output: json.dump(payload, output, ensure_ascii=False, indent=2)

if __name__ == '__main__': main()
