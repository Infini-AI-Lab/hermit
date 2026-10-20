"""Render the two cards of cards.html to GIFs, one per card: drive the page's timeline frame by frame, screenshot each
card element, assemble with Pillow.

Usage: python export-cards.py cards.html ../../../../assets 20 [scale]
Writes <out dir>/install-and-use.gif (the #cmd card) and <out dir>/training-loop.gif (the #code card).
Needs playwright (with its chromium) and pillow."""
import io
import sys
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

page_path, out_dir, fps = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), int(sys.argv[3])
scale = float(sys.argv[4]) if len(sys.argv) > 4 else 2
CARDS = {"cmd": "install-and-use.gif", "code": "training-loop.gif"}
frames = {k: [] for k in CARDS}
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1240, "height": 1100}, device_scale_factor=scale, color_scheme="light")
    page.goto(page_path.as_uri())
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(300)
    duration = float(page.evaluate("document.getElementById('timeline').max"))
    n = int(duration * fps) + 1
    for i in range(n):
        t = min(duration, i / fps)
        page.evaluate("t => { const s = document.getElementById('timeline'); s.value = t; s.dispatchEvent(new Event('input')); }", t)
        for card in CARDS:
            frames[card].append(Image.open(io.BytesIO(page.locator(f"#{card}").screenshot())).convert("RGB"))
    browser.close()
for card, name in CARDS.items():
    quant = [f.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames[card]]
    out = out_dir / name
    quant[0].save(out, save_all=True, append_images=quant[1:], duration=int(1000 / fps), loop=0, optimize=True)
    w, h = frames[card][0].size
    print(f"{out}: {len(quant)} frames, {w}x{h}, {out.stat().st_size} bytes, {duration:.1f}s")
