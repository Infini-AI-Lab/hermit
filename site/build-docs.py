#!/usr/bin/env python3
"""Build the local guide from English Markdown sources, without extra dependencies."""
from html import escape
import importlib.util
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_sync', ROOT/'site/sync-docs.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
PAGES = [('index', 'Introduction'), ('quickstart', 'Quickstart'), ('components', 'Components'),
         ('architectures', 'Architectures'), ('animation-workshop', 'Animation Workshop'), ('deployment', 'Deployment'),
         ('evolution', 'Evolution & LearningMaterial'), ('configuration', 'Configuration'), ('status', 'Status & limits')]
dest = ROOT/'site/dist/docs'
dest.mkdir(exist_ok=True)
for index, (slug, title) in enumerate(PAGES):
    text = (ROOT/f'docs/guide/{slug}.md').read_text()
    body = renderer.render(text)
    for other, _ in PAGES:
        body = body.replace(f'href="../{other}.html"', f'href="{other}.html"')
    headings = []
    def heading(match):
        label = match[1]
        anchor = re.sub(r'[^a-z0-9]+', '-', re.sub('<[^>]+>', '', label).lower()).strip('-')
        headings.append((anchor, label))
        return f'<h2 id="{anchor}">{label}</h2>'
    body = re.sub(r'<h2>(.*?)</h2>', heading, body)
    nav = ''.join(f'<a href="{key}.html"'+(' aria-current="page"' if key == slug else '')+f'>{label}</a>' for key, label in PAGES)
    toc = ''.join(f'<a href="#{anchor}">{label}</a>' for anchor, label in headings)
    following = PAGES[(index+1) % len(PAGES)]
    (dest/f'{slug}.html').write_text(f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Harness Adapter Docs</title><link rel="stylesheet" href="docs.css"><script defer src="docs.js"></script></head>
<body><header><a class="brand" href="../">H<span>↗</span> Harness Adapter</a><a href="../">Design notebook ↗</a></header>
<div class="layout"><aside class="navigation"><div class="eyebrow">DOCUMENTATION / V0</div><nav aria-label="Documentation">{nav}</nav><div class="note">Local prototype<br>English sources in docs/guide</div></aside>
<main><div class="eyebrow">GUIDE / {index+1:02d}</div>{body}<footer><a href="{slug}.md">Markdown source ↓</a><a href="{following[0]}.html">Next: {following[1]} →</a></footer></main>
<aside class="toc"><div class="eyebrow">ON THIS PAGE</div>{toc}</aside></div></body></html>''')
    (dest/f'{slug}.md').write_text(text)
print(f'Built {len(PAGES)} documentation pages at site/dist/docs/')
