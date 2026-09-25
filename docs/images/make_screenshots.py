"""Regenerate the README's screenshots from the demo catalogue.

    pip install playwright pillow && playwright install chromium
    python docs/images/make_screenshots.py

Each picture is taken in light and dark, framed (rounded corners, shadow,
gradient) and written to docs/images/<name>-light.png / -dark.png.
"""
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
logging.disable(logging.WARNING)
import uvicorn  # noqa: E402 (after the path is set)
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402 (after the path is set)
from playwright.sync_api import sync_playwright  # noqa: E402 (after the path is set)

from sieve import demo  # noqa: E402 (after the path is set)
from sieve.app import create_app  # noqa: E402 (after the path is set)
from tests.support import offline_config  # noqa: E402 (after the path is set)

app = create_app(offline_config(tempfile.mkdtemp()), start_worker=False)
db = app.state.db
demo.seed_demo(db, 160)
db.set_setting("dismissed_notices", ["playback", "demo", "short"])
threading.Thread(target=uvicorn.Server(uvicorn.Config(app, port=8451, log_level="error")).run, daemon=True).start()
time.sleep(2)
B = "http://127.0.0.1:8451"
OUT = str(ROOT / "docs" / "images")

def frame(src, dst, dark):
    """Rounded corners, a soft shadow, a gradient backdrop."""
    shot = Image.open(src).convert("RGBA")
    w, h = shot.size
    pad, r = 48, 14
    top, bottom = ((34, 32, 64), (20, 20, 34)) if dark else ((232, 231, 250), (214, 226, 240))
    bg = Image.new("RGBA", (w + 2 * pad, h + 2 * pad))
    draw = ImageDraw.Draw(bg)
    for y in range(bg.height):
        t = y / bg.height
        draw.line([(0, y), (bg.width, y)], fill=(*(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)), 255))
    shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([pad, pad + 10, pad + w, pad + h + 10], r, fill=(0, 0, 0, 110 if dark else 60))
    bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(16)))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], r, fill=255)
    bg.paste(shot, (pad, pad), mask)
    bg.convert("RGB").save(dst, optimize=True)

def take(page, name, dark, locator=None, clip=None):
    raw = f"/tmp/raw-{name}.png"
    if locator:
        page.locator(locator).first.screenshot(path=raw)
    else:
        page.screenshot(path=raw, clip=clip)
    frame(raw, f"{OUT}/{name}-{'dark' if dark else 'light'}.png", dark)

with sync_playwright() as p:
    b = p.chromium.launch()
    for dark in (False, True):
        ctx = b.new_context(color_scheme="dark" if dark else "light", viewport={"width": 1360, "height": 860},
                            device_scale_factor=1)
        pg = ctx.new_page()
        pg.goto(B + "/")
        pg.wait_for_timeout(500)
        take(pg, "home", dark, clip={"x": 0, "y": 0, "width": 1360, "height": 820})
        card = pg.locator(".grid [data-video]").nth(1)
        card.locator(".why details summary").click()
        pg.wait_for_timeout(200)
        take(pg, "why", dark, locator=".grid [data-video] >> nth=1")
        pg.goto(B + "/settings")
        pg.wait_for_timeout(400)
        take(pg, "find-videos", dark, locator="#pulling")
        pg.locator("[data-help='education'][data-direction='min']").click()
        pg.wait_for_timeout(400)
        pg.locator("[data-range-open]").click()
        pg.wait_for_timeout(700)
        pg.locator(".numberline button:not(.empty)").nth(2).click()
        pg.wait_for_timeout(400)
        pg.locator(".numberline button:not(.empty)").nth(-2).click()
        pg.wait_for_timeout(500)
        pg.evaluate("document.querySelectorAll('.range-compare ul').forEach(u => [...u.children].slice(4).forEach(li => li.remove()))")
        take(pg, "score-range", dark, locator=".help-pop")
        pg.goto(B + "/library?q=kernel")
        pg.wait_for_timeout(400)
        take(pg, "library", dark, locator="#search")
        ph = ctx.new_page()
        ph.set_viewport_size({"width": 390, "height": 800})
        ph.goto(B + "/")
        ph.wait_for_timeout(400)
        take(ph, "phone", dark, clip={"x": 0, "y": 0, "width": 390, "height": 780})
        ctx.close()
    b.close()
