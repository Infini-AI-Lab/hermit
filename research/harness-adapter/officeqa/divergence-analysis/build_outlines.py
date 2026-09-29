"""One line per trajectory step (first line of each THINK/SAY/RUN entry, clipped), so an analyst can
navigate a long digest and then open the full step with: grep -n '^\[123\] ' <digest>."""
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent
STEP = re.compile(r'^\[(\d+)\] ')
for digest in sorted(OUT.glob('digests/*/*.md')):
    if digest.name.endswith('.outline.md'):
        continue
    lines = digest.read_text(errors='ignore').splitlines()
    head = [l for l in lines[:8] if l and not l.startswith('## ')]
    steps = [l[:240] for l in lines if STEP.match(l)]
    digest.with_suffix('.outline.md').write_text('\n'.join(head + ['', '## Outline (one line per step)', ''] + steps) + '\n')
print('done')
