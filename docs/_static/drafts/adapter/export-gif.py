"""Render the harness-adapter animation to a GIF: drive the page's timeline input frame by frame, screenshot the
figure (scene + captions), and assemble with Pillow.

Usage: python export-gif.py harness-adapter-animation.html ../../../../assets/harness-adapter.gif 20 9 [light|dark]
Needs playwright (with its chromium) and pillow."""
import sys
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

page_path, out, fps, duration = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
scheme = sys.argv[5] if len(sys.argv) > 5 else "light"
frames = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1080, "height": 600}, device_scale_factor=1, color_scheme=scheme)
    page.goto(page_path.as_uri())
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.add_style_tag(content=".controls, .draft-note, .icon-study-link { display: none !important; } "
                               "main { padding: 16px 0 8px; } body { min-height: 0; display: block; }")
    page.wait_for_timeout(300)
    # the timeline input event pauses playback and drives render(t)
    n = int(duration * fps) + 1
    for i in range(n):
        t = min(duration, i / fps)
        page.evaluate("t => { const s = document.getElementById('timeline'); s.value = t; s.dispatchEvent(new Event('input')); }", t)
        png = page.locator("figure").screenshot()
        frames.append(Image.open(__import__("io").BytesIO(png)).convert("RGB"))
    browser.close()
w, h = frames[0].size
quant = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
quant[0].save(out, save_all=True, append_images=quant[1:], duration=int(1000 / fps), loop=0, optimize=True)
print(f"{out}: {len(frames)} frames, {w}x{h}, {out.stat().st_size} bytes")
