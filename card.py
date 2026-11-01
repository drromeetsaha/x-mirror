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
  <div class="foot"><span>{date}</span><span>{footer}</span></div>
<script>
  // Shrink the text until it fits the space between header and footer.
  const main = document.querySelector('.main'), t = document.getElementById('t');
  let size = {start};
  t.style.fontSize = size + 'px';
  while (size > 26 && t.scrollHeight > main.clientHeight - 80) {{
    size -= 2; t.style.fontSize = size + 'px';
  }}
  document.body.dataset.size = size;  // read back by card_font_size()
</script>
</body></html>"""

THEMES = {
    "dark": dict(bg="#0b0d10", fg="#f2f4f5", muted="#8b98a5",
                 line="#2a3036", accent="#1d9bf0"),
    "light": dict(bg="#ffffff", fg="#0f1419", muted="#536471",
                  line="#e1e8ed", accent="#1d9bf0"),
}

GOLD = "#e3b55b"  # The ChartRoom logo colour

# A chart photo inside The ChartRoom frame: logo and date, the post's first
# line as a title, then the chart. The chart's position is computed here, not
# by the browser, so a Reel can animate exactly that area.
FRAME_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: {w}px; height: {h}px; overflow: hidden; }}
  body {{
    background: {bg};
    color: {fg};
    position: relative;
    font-family: Inter, -apple-system, "Segoe UI", "Noto Sans", "DejaVu Sans",
                 sans-serif, "Apple Color Emoji", "Noto Color Emoji";
  }}
  .logo {{ position: absolute; left: {pad}px; top: {head_top}px; height: 84px; }}
  .date {{ position: absolute; right: {pad}px; top: {head_top}px; line-height: 84px;
           font-size: 30px; color: {muted}; }}
  .title {{ position: absolute; left: {pad}px; right: {pad}px; top: {title_top}px;
            height: {title_h}px; display: flex; align-items: flex-end;
            border-left: 6px solid {gold}; padding-left: 28px; }}
  #t {{ white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.22;
        font-weight: 700; }}
  .tag {{ color: {gold}; }}
  .chart {{ position: absolute; left: {cx}px; top: {cy}px; width: {cw}px;
            height: {ch}px; box-shadow: 0 0 0 2px {line}; }}
  .foot {{ position: absolute; left: {pad}px; right: {pad}px; top: {foot_top}px;
           display: flex; justify-content: space-between; font-size: 30px;
           color: {muted}; border-top: 2px solid {line}; padding-top: 28px; }}
  .foot b {{ color: {gold}; font-weight: 600; }}
</style></head><body>
  <img class="logo" src="{logo}">
  <div class="date">{date}</div>
  <div class="title"><div id="t">{title}</div></div>
  <img class="chart" src="{chart}">
  {foot}
<script>
  // Shrink the title until it fits its band.
  const box = document.querySelector('.title'), t = document.getElementById('t');
  let size = {font};
  t.style.fontSize = size + 'px';
  while (size > 30 && t.scrollHeight > box.clientHeight) {{
    size -= 2; t.style.fontSize = size + 'px';
  }}
</script>
</body></html>"""

FRAME_LAYOUTS = {
    # Instagram feed post, 4:5. The profile grid trims ~35px off each side.
    "feed": dict(w=1080, h=1350, pad=60, head_top=60, title_top=176, title_h=240,
                 chart=(48, 456, 984, 744), foot_top=1236, font=64),
    # Reel, 9:16. Instagram's own buttons cover ~250px at the top and ~420px
    # at the bottom, and the profile grid shows only the middle 3:4.
    "reel": dict(w=1080, h=1920, pad=60, head_top=280, title_top=396, title_h=320,
                 chart=(0, 756, 1080, 744), foot_top=None, font=72),
}


URL_RE = re.compile(r"https?://(?:www\.)?([^\s/]+)(/\S*)?")


def _highlight(text):
    """Escape text, colour #hashtags, @mentions and $cashtags, and shorten
    links to their site (links aren't clickable on an image anyway)."""
    links = []

    def stash(m):
        links.append(m.group(1) + ("/…" if m.group(2) and len(m.group(2)) > 1 else ""))
        return "\x00{}\x00".format(len(links) - 1)

    escaped = html.escape(URL_RE.sub(stash, text))
    escaped = re.sub(r"(?<![\w&])([#@$][A-Za-z_][\w.]*)",
                     r'<span class="tag">\1</span>', escaped)
    return re.sub("\x00(\\d+)\x00", lambda m: '<span class="tag">{}</span>'.format(
        html.escape(links[int(m.group(1))])), escaped)


def _chrome():
    for c in CHROME_CANDIDATES:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    raise RuntimeError("Chrome not found; set CHROME_PATH")


def _chrome_run(page, width, height, *args):
    """Load an HTML page in headless Chrome; returns (stdout, temp dir)."""
    tmp = tempfile.mkdtemp()
    page_path = os.path.join(tmp, "page.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(page)
    out = subprocess.run(
        [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-sandbox", "--force-device-scale-factor=1",
         "--allow-file-access-from-files", "--virtual-time-budget=5000",
         "--window-size={},{}".format(width, height)] + list(args)
        + ["file://" + page_path],
        check=True, capture_output=True, timeout=90, text=True)
    return out.stdout, tmp


def _screenshot(page, out_path, width, height):
    """Render an HTML page with headless Chrome and save it as a JPEG."""
    with tempfile.TemporaryDirectory() as tmp:
        png_path = os.path.join(tmp, "page.png")
        _, page_dir = _chrome_run(page, width, height, "--screenshot=" + png_path)
        shutil.rmtree(page_dir, ignore_errors=True)
        # Instagram's API only accepts JPEG.
        Image.open(png_path).convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


def _even(n):
    return int(n) // 2 * 2


def render_chart_frame(chart_path, out_path, title, handle, logo_path, date_text,
                       layout="feed", theme="dark", ask="📤 Send to a trader friend"):
    """Write the chart inside The ChartRoom frame as a JPEG.

    Returns the chart's (x, y, width, height) inside the frame.
    """
    spec = FRAME_LAYOUTS[layout]
    colors = THEMES.get(theme, THEMES["dark"])
    ax, ay, aw, ah = spec["chart"]
    w, h = Image.open(chart_path).size
    scale = min(aw / w, ah / h)
    cw, ch = _even(w * scale), _even(h * scale)
    # Wide charts leave spare height: keep most of it below, so the chart
    # sits close to its title.
    cx, cy = _even(ax + (aw - cw) / 2), _even(ay + (ah - ch) * 0.25)
    foot = ""
    if spec["foot_top"]:
        foot = ('<div class="foot"><span>{}</span>'
                '<span>Follow <b>@{}</b></span></div>'.format(
                    html.escape(ask), html.escape(handle)))
    page = FRAME_TEMPLATE.format(
        w=spec["w"], h=spec["h"], pad=spec["pad"], head_top=spec["head_top"],
        title_top=spec["title_top"], title_h=spec["title_h"], font=spec["font"],
        foot_top=spec["foot_top"] or 0, cx=cx, cy=cy, cw=cw, ch=ch,
        logo="file://" + os.path.abspath(logo_path),
        chart="file://" + os.path.abspath(chart_path),
        title=_highlight(title), date=html.escape(date_text), foot=foot,
        gold=GOLD, **colors)
    _screenshot(page, out_path, spec["w"], spec["h"])
    return cx, cy, cw, ch


def _card_page(text, name, handle, avatar_url=None, created_at=None,
               quote=None, theme="dark", footer=None, size=None):
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
        date=when.strftime("%-d %b %Y"),
        footer=html.escape(footer or "Follow @{} on X".format(handle)),
        start=size or 68, **colors)
    return page


def card_font_size(text, name, handle, **kw):
    """The font size a card's text ends up at once shrunk to fit. The pages
    of a carousel all use the smallest, so they match."""
    dom, page_dir = _chrome_run(_card_page(text, name, handle, **kw),
                                WIDTH, HEIGHT, "--dump-dom")
    shutil.rmtree(page_dir, ignore_errors=True)
    found = re.search(r'data-size="(\d+)"', dom)
    return int(found.group(1)) if found else 26


def render_card(text, out_path, name, handle, avatar_url=None, created_at=None,
                quote=None, theme="dark", footer=None, size=None):
    """Write a JPEG card to out_path.

    quote: optional dict with name, handle, text for a quoted post.
    footer: bottom-right text; defaults to "Follow @handle on X".
    size: largest font size to try (px); the text shrinks from there to fit.
    """
    page = _card_page(text, name, handle, avatar_url, created_at, quote, theme,
                      footer, size)
    return _screenshot(page, out_path, WIDTH, HEIGHT)


# Where a 4:5 card sits on a 9:16 Reel or Story: a little smaller than full
# width, so its header clears Instagram's buttons at the top and most of it
# sits above the caption at the bottom.
CARD_ON_TALL = (54, 300, 972, 1214)


def card_on_tall(card_path, out_path, theme="dark", background_only=False):
    """Write a 1080x1920 JPEG with the card on it (a Story), or only the
    background (the still layer of a card Reel). Returns the card's rect."""
    bg = THEMES.get(theme, THEMES["dark"])["bg"]
    canvas = Image.new("RGB", (1080, 1920), bg)
    x, y, w, h = CARD_ON_TALL
    if not background_only:
        canvas.paste(Image.open(card_path).convert("RGB").resize((w, h)), (x, y))
    canvas.save(out_path, "JPEG", quality=92)
    return CARD_ON_TALL


RECAP_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: {w}px; height: {h}px; overflow: hidden; }}
  body {{
    background: {bg}; color: {fg};
    font-family: Inter, -apple-system, "Segoe UI", "Noto Sans", "DejaVu Sans",
                 sans-serif, "Apple Color Emoji", "Noto Color Emoji";
    padding: 70px 70px 64px; display: flex; flex-direction: column;
  }}
  .logo {{ height: 96px; align-self: flex-start; }}
  h1 {{ margin-top: 56px; font-size: 84px; line-height: 1.08; font-weight: 800;
        border-left: 8px solid {gold}; padding-left: 30px; }}
  .range {{ margin-top: 22px; padding-left: 38px; font-size: 38px; color: {gold};
            font-weight: 600; }}
  .main {{ flex: 1; overflow: hidden; padding: 44px 0 24px; }}
  ol {{ list-style: none; counter-reset: n; }}
  li {{ counter-increment: n; display: flex; gap: 0.7em; line-height: 1.25;
        font-weight: 500; padding: 0.42em 0; border-bottom: 2px solid {line}; }}
  li::before {{ content: counter(n); color: {gold}; font-weight: 800;
                min-width: 1.3em; }}
  li .t {{ flex: 1; display: -webkit-box; -webkit-line-clamp: 2;
           -webkit-box-orient: vertical; overflow: hidden; }}
  li .d {{ color: {muted}; white-space: nowrap; font-size: 0.8em;
           padding-top: 0.12em; }}
  .foot {{ display: flex; justify-content: space-between; font-size: 32px;
           color: {muted}; border-top: 2px solid {line}; padding-top: 30px; }}
  .foot b {{ color: {gold}; font-weight: 600; }}
</style></head><body>
  <img class="logo" src="{logo}">
  <h1>{heading}</h1>
  <div class="range">{range}</div>
  <div class="main"><ol id="t">{items}</ol></div>
  <div class="foot"><span>Swipe for the charts →</span><span>Follow <b>@{handle}</b></span></div>
<script>
  const main = document.querySelector('.main'), t = document.getElementById('t');
  let size = 44;
  t.style.fontSize = size + 'px';
  const room = main.clientHeight - 68;  // less the padding
  while (size > 26 && t.scrollHeight > room) {{
    size -= 2; t.style.fontSize = size + 'px';
  }}
</script>
</body></html>"""


def render_recap_cover(items, date_range, handle, logo_path, out_path,
                       heading="This week's top charts", theme="dark"):
    """The first slide of the weekly recap: a numbered list of the charts.
    items: (title, date) pairs."""
    colors = THEMES.get(theme, THEMES["dark"])
    items = "".join(
        '<li><div class="t"><span>{}</span></div><span class="d">{}</span></li>'.format(
            _highlight(t), html.escape(d)) for t, d in items)
    page = RECAP_TEMPLATE.format(
        w=WIDTH, h=HEIGHT, logo="file://" + os.path.abspath(logo_path),
        heading=html.escape(heading), range=html.escape(date_range), items=items,
        handle=html.escape(handle), gold=GOLD, **colors)
    return _screenshot(page, out_path, WIDTH, HEIGHT)
