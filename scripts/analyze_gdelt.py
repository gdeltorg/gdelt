#!/usr/bin/env python3
import html
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE = 'https://data.gdeltproject.org/gdeltv2/'
USER_AGENT = 'gdeltorg-dashboard/1.0 (+https://github.com/gdeltorg/gdelt)'


def download(url, timeout=90):
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=timeout) as resp:
        return resp.read()


def discover_latest_files():
    text = download(BASE + 'lastupdate.txt').decode('utf-8', 'replace')
    files = {}
    for url in re.findall(r'https?://[^\s]+', text):
        low = url.lower().rstrip('.,')
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
        with zf.open(zf.namelist()[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding='utf-8', errors='replace')
            for line in stream:
                yield line.rstrip('\n').split('\t')


def count_records(url):
    return sum(1 for _ in iter_rows(url))


def clean_theme(value):
    value = value.replace('_', ' ')
    return re.sub(r'\s*\([^)]*\)', '', value).strip()


def domain(url):
    try:
        return urlparse(url).netloc.lower().removeprefix('www.')
    except ValueError:
        return ''


def extract_meta(url):
    """GKG has URL/source fields, but no article headline or abstract.
    Fetch the public page metadata so the dashboard can show readable cards.
    """
    result = {'headline': '', 'summary': ''}
    if not url.startswith(('http://', 'https://')):
        return result
    try:
        raw = download(url, timeout=12)[:750_000].decode('utf-8', 'replace')
        def meta(*names):
            names_re = '|'.join(re.escape(name) for name in names)
            pattern = rf'<meta[^>]+(?:property|name)=["\'](?:{names_re})["\'][^>]+content=["\']([^"\']*)'
            match = re.search(pattern, raw, re.I)
            if not match:
                pattern = rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\'](?:{names_re})["\']'
                match = re.search(pattern, raw, re.I)
            return html.unescape(re.sub(r'\s+', ' ', match.group(1))).strip() if match else ''
        result['headline'] = meta('og:title', 'twitter:title')
        result['summary'] = meta('og:description', 'twitter:description', 'description')
        if not result['headline']:
            match = re.search(r'<title[^>]*>(.*?)</title>', raw, re.I | re.S)
            if match:
                result['headline'] = html.unescape(re.sub(r'\s+', ' ', match.group(1))).strip()
    except Exception:
        pass
    return result


def rank_news(gkg_url):
    ranked = []
    for idx, row in enumerate(iter_rows(gkg_url)):
        if idx >= 50000 or len(row) < 10:
            break
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
            'source': document_url,
            'source_name': source_name or domain(document_url),
            'date': row[0][:12] if row else '',
            'score': score,
            'themes': themes[:12]
        })
    ranked.sort(key=lambda item: item['score'], reverse=True)
    selected = ranked[:10]
    for item in selected:
        metadata = extract_meta(item['url'])
        item['title'] = metadata['headline'] or item['source_name'] or domain(item['url']) or 'Untitled story'
        item['summary'] = metadata['summary'] or 'GDELT 已发现该报道；原文摘要由来源网站提供。'
        time.sleep(0.15)
    return selected


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
    with open('data/latest.json', 'w', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
