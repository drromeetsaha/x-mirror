"""Render a post as a 1080x1350 image card (Instagram's 4:5 portrait size).

The card is laid out as HTML and screenshotted with headless Chrome, which
handles fonts, emoji and line wrapping far better than drawing text by hand.
"""

import html
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

from PIL import Image

WIDTH, HEIGHT = 1080, 1350

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: {w}px; height: {h}px; }}
  body {{
    background: {bg};
    color: {fg};
    font-family: Inter, -apple-system, "Segoe UI", "Noto Sans", "DejaVu Sans",
                 sans-serif, "Apple Color Emoji", "Noto Color Emoji";
    padding: 96px 90px 80px;
    display: flex; flex-direction: column;
  }}
  .head {{ display: flex; align-items: center; gap: 28px; }}
  .avatar {{
    width: 112px; height: 112px; border-radius: 50%; flex: none;
    background: {accent} center/cover no-repeat;
    display: flex; align-items: center; justify-content: center;
    font-size: 52px; font-weight: 700; color: #fff;
  }}
  .name {{ font-size: 44px; font-weight: 700; line-height: 1.2; }}
  .handle {{ font-size: 36px; color: {muted}; margin-top: 4px; }}
  .main {{ flex: 1; display: flex; flex-direction: column;
           justify-content: center; overflow: hidden; padding: 40px 0; }}
  .text {{ white-space: pre-wrap; overflow-wrap: anywhere;
           line-height: 1.38; font-weight: 500; }}
  .tag {{ color: {accent}; }}
  .quote {{ margin-top: 1.1em; border: 3px solid {line}; border-radius: 30px;
            padding: 0.9em 1em; font-size: 0.72em; }}
  .quote .who {{ color: {muted}; margin-bottom: 0.4em; }}
  .quote .who b {{ color: {fg}; }}
  .foot {{ display: flex; justify-content: space-between; font-size: 30px;
           color: {muted}; border-top: 2px solid {line}; padding-top: 34px; }}
</style></head><body>
  <div class="head">
    <div class="avatar" style="{avatar_style}">{avatar_letter}</div>
    <div><div class="name">{name}</div><div class="handle">@{handle}</div></div>
  </div>
  <div class="main"><div class="text" id="t">{text}{quote}</div></div>
  <div class="foot"><span>{date}</span><span>Follow @{handle} on X</span></div>
<script>
  // Shrink the text until it fits the space between header and footer.
  const main = document.querySelector('.main'), t = document.getElementById('t');
  let size = 68;
  t.style.fontSize = size + 'px';
  while (size > 26 && t.scrollHeight > main.clientHeight - 80) {{
    size -= 2; t.style.fontSize = size + 'px';
  }}
</script>
</body></html>"""

THEMES = {
    "dark": dict(bg="#0b0d10", fg="#f2f4f5", muted="#8b98a5",
                 line="#2a3036", accent="#1d9bf0"),
    "light": dict(bg="#ffffff", fg="#0f1419", muted="#536471",
                  line="#e1e8ed", accent="#1d9bf0"),
}


def _highlight(text):
    """Escape text and colour #hashtags, @mentions and $cashtags."""
    escaped = html.escape(text)
    return re.sub(r"(?<![\w&])([#@$][A-Za-z_][\w.]*)",
                  r'<span class="tag">\1</span>', escaped)


def _chrome():
    for c in CHROME_CANDIDATES:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    raise RuntimeError("Chrome not found; set CHROME_PATH")


def render_card(text, out_path, name, handle, avatar_url=None, created_at=None,
                quote=None, theme="dark"):
    """Write a JPEG card to out_path.

    quote: optional dict with name, handle, text for a quoted post.
    """
    colors = THEMES.get(theme, THEMES["dark"])
    quote_html = ""
    if quote:
        quote_html = (
            '<div class="quote"><div class="who"><b>{}</b> @{}</div>{}</div>'
            .format(html.escape(quote["name"]), html.escape(quote["handle"]),
                    _highlight(quote["text"])))
    if avatar_url:
        avatar_style = "background-image:url('{}')".format(html.escape(avatar_url))
        avatar_letter = ""
    else:
        avatar_style = ""
        avatar_letter = html.escape(name[:1].upper())
    when = created_at or datetime.now()
    page = TEMPLATE.format(
        w=WIDTH, h=HEIGHT, name=html.escape(name), handle=html.escape(handle),
        avatar_style=avatar_style, avatar_letter=avatar_letter,
        text=_highlight(text), quote=quote_html,
        date=when.strftime("%-d %b %Y"), **colors)

    with tempfile.TemporaryDirectory() as tmp:
        page_path = os.path.join(tmp, "card.html")
        png_path = os.path.join(tmp, "card.png")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(page)
        subprocess.run(
            [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--no-sandbox", "--force-device-scale-factor=1",
             "--virtual-time-budget=5000",
             "--window-size={},{}".format(WIDTH, HEIGHT),
             "--screenshot=" + png_path, "file://" + page_path],
            check=True, capture_output=True, timeout=90)
        # Instagram's API only accepts JPEG.
        Image.open(png_path).convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path
