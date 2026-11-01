"""Mirror new X posts to a Facebook Page and Instagram.

Runs every few minutes from GitHub Actions (.github/workflows/mirror.yml).
Each run reads only the posts newer than the last one it saw, then publishes
each one to Facebook and Instagram. Progress lives in state.json, which the
workflow commits back, so nothing is posted twice.

Environment:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   X app user keys
  FB_PAGE_ID, FB_PAGE_TOKEN, IG_USER_ID                       Meta IDs/token
  DRY_RUN=1        read from X but print what would be posted; save nothing
  TEST_POST_ID=id  mirror this one post now, ignoring state
"""

import hashlib
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image
from requests_oauthlib import OAuth1

from card import (CARD_ON_TALL, THEMES, URL_RE, card_font_size, card_on_tall,
                  render_card, render_chart_frame, render_recap_cover)
from reel import join_reels, render_reel

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")

X_API = "https://api.x.com/2"
MAX_ATTEMPTS = 5
IG_CAPTION_LIMIT = 2200
CARD_CHARS = 420  # about what a card holds at a readable size
FB_REEL_MAX = 90  # seconds; Facebook doesn't take longer Reels
ALT_CHARS = 100  # alt text is read out in full, so keep it short
DRY_RUN = os.environ.get("DRY_RUN") == "1"
WORK = tempfile.mkdtemp(prefix="x-mirror-")  # charts, frames and Reels for this run

POST_FIELDS = {
    "tweet.fields": "created_at,entities,note_tweet,referenced_tweets,attachments,author_id",
    "expansions": "attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
    "media.fields": "type,url,variants,preview_image_url,width,height",
    "user.fields": "name,username",
}

SECRETS = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET",
           "FB_PAGE_ID", "FB_PAGE_TOKEN", "IG_USER_ID"]


# ---------------------------------------------------------------- storage

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_state(state):
    if DRY_RUN:
        return
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- X

def x_get(path, params=None):
    auth = OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                  os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])
    resp = requests.get(X_API + path, params=params, auth=auth, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError("X API {} on {}: {}".format(
            resp.status_code, path, resp.text[:500]))
    return resp.json()


def refresh_profile(state):
    data = x_get("/users/me", {"user.fields": "profile_image_url,name,username"})["data"]
    state["user"] = {
        "id": data["id"],
        "name": data["name"],
        "username": data["username"],
        "avatar": (data.get("profile_image_url") or "").replace("_normal", "_400x400"),
        "checked": now_utc().isoformat(),
    }


def clean_text(text, entities, quoted_id=None):
    """Swap t.co links for real ones and drop links to attached media or the
    quoted post, which the mirror shows in other ways."""
    for u in (entities or {}).get("urls", []):
        short = u.get("url", "")
        real = u.get("unwound_url") or u.get("expanded_url") or short
        is_media = u.get("media_key") or "/photo/" in real or "/video/" in real
        is_quote = quoted_id and "/status/{}".format(quoted_id) in real
        text = text.replace(short, "" if (is_media or is_quote) else real)
    return html.unescape(text).strip()


def media_item(m):
    if m["type"] == "photo":
        return {"type": "photo", "url": m["url"]}
    mp4s = [v for v in m.get("variants", []) if v.get("content_type") == "video/mp4"]
    if not mp4s:
        return None
    best = max(mp4s, key=lambda v: v.get("bit_rate", 0))
    return {"type": "video", "url": best["url"]}


def normalize(p, includes):
    media_by_key = {m["media_key"]: m for m in includes.get("media", [])}
    tweets_by_id = {t["id"]: t for t in includes.get("tweets", [])}
    users_by_id = {u["id"]: u for u in includes.get("users", [])}

    if "note_tweet" in p:  # long post: full text lives here
        text, entities = p["note_tweet"]["text"], p["note_tweet"].get("entities")
    else:
        text, entities = p["text"], p.get("entities")
    quoted_id = next((r["id"] for r in p.get("referenced_tweets", [])
                      if r["type"] == "quoted"), None)

    media = [media_item(media_by_key[k])
             for k in p.get("attachments", {}).get("media_keys", [])
             if k in media_by_key]

    quote = None
    if quoted_id and quoted_id in tweets_by_id:
        q = tweets_by_id[quoted_id]
        author = users_by_id.get(q.get("author_id"), {})
        quote = {"name": author.get("name", ""),
                 "handle": author.get("username", ""),
                 "text": clean_text(q["text"], q.get("entities"))}

    return {"id": p["id"], "created_at": p.get("created_at"),
            "text": clean_text(text, entities, quoted_id),
            "media": [m for m in media if m], "quote": quote}


def fetch_new_posts(state):
    """Original posts and quote posts newer than the last one seen."""
    params = dict(POST_FIELDS, exclude="replies,retweets", max_results=100)
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    else:
        params["start_time"] = state["started_at"]
    data = x_get("/users/{}/tweets".format(state["user"]["id"]), params)
    newest = data.get("meta", {}).get("newest_id")
    if newest:
        state["since_id"] = newest
    includes = data.get("includes", {})
    return [normalize(p, includes) for p in reversed(data.get("data", []))]


def fetch_one_post(post_id):
    data = x_get("/tweets/{}".format(post_id), POST_FIELDS)
    return normalize(data["data"], data.get("includes", {}))


# ---------------------------------------------------------------- Meta

def graph(method, path, data=None, files=None, params=None):
    if DRY_RUN:
        print("    [dry run] {} {} {}".format(method, path, {
            k: (str(v)[:80]) for k, v in (data or {}).items()}))
        return {"id": "dry-run", "video_id": "dry-run", "status_code": "FINISHED",
                "images": [{"width": 1, "source": "https://example.com/dry.jpg"}]}
    cfg = load_json(CONFIG_PATH, {})
    url = "https://graph.facebook.com/{}/{}".format(cfg.get("graph_version", "v26.0"), path)
    payload = dict(data or {}, access_token=os.environ["FB_PAGE_TOKEN"])
    if method == "GET":
        resp = requests.get(url, params=dict(params or {}, **payload), timeout=60)
    else:
        resp = requests.post(url, data=payload, files=files, timeout=300)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or "error" in body:
        msg = body.get("error", {}).get("message") or resp.text[:300]
        raise RuntimeError("Meta API {} on {}: {}".format(resp.status_code, path, msg))
    return body


def rupload(kind, media_id, video_path):
    """Send a local video to Meta's upload host. kind is "ig-api-upload"
    (Instagram Reel container) or "video-upload" (Facebook Reel)."""
    if DRY_RUN:
        print("    [dry run] upload {} to {} {}".format(
            os.path.basename(video_path), kind, media_id))
        return
    cfg = load_json(CONFIG_PATH, {})
    with open(video_path, "rb") as f:
        body = f.read()
    resp = requests.post(
        "https://rupload.facebook.com/{}/{}/{}".format(
            kind, cfg.get("graph_version", "v26.0"), media_id),
        data=body, timeout=300, headers={
            "Authorization": "OAuth " + os.environ["FB_PAGE_TOKEN"],
            "offset": "0", "file_size": str(len(body))})
    try:
        ok = resp.json().get("success")
    except ValueError:
        ok = False
    if resp.status_code >= 400 or not ok:
        raise RuntimeError("Meta upload {} on {}: {}".format(
            resp.status_code, kind, resp.text[:300]))


def page():
    return os.environ["FB_PAGE_ID"]


def ig():
    return os.environ["IG_USER_ID"]


def download_image(url):
    sep = "&" if "?" in url else "?"
    resp = requests.get(url + sep + "name=large", timeout=60)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content))


def fit_for_instagram(img, pad_color):
    """Instagram feed images must be between 4:5 (tall) and 1.91:1 (wide).
    Pad anything outside that range rather than cropping it."""
    img = img.convert("RGB")
    w, h = img.size
    if w / h < 0.8:
        canvas = Image.new("RGB", (round(h * 0.8), h), pad_color)
        canvas.paste(img, ((canvas.width - w) // 2, 0))
        img = canvas
    elif w / h > 1.91:
        canvas = Image.new("RGB", (w, round(w / 1.91)), pad_color)
        canvas.paste(img, (0, (canvas.height - h) // 2))
        img = canvas
    if img.width > 1440:
        img = img.resize((1440, round(img.height * 1440 / img.width)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def fb_photo(jpeg):
    """Upload an image to the Page as an unpublished photo (not shown on the
    Page by itself); returns its ID."""
    return graph("POST", "{}/photos".format(page()), data={"published": "false"},
                 files={"source": ("image.jpg", jpeg, "image/jpeg")})["id"]


def host_image(jpeg):
    """Instagram fetches images from a public URL: an unpublished Page photo
    gives the image one."""
    info = graph("GET", fb_photo(jpeg), params={"fields": "images"})
    return max(info["images"], key=lambda i: i["width"])["source"]


def wait_until_ready(container_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = graph("GET", container_id, params={"fields": "status_code"})
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError("Instagram could not process media (status {})".format(code))
        time.sleep(5)
    raise RuntimeError("Instagram media still processing after {}s".format(timeout))


def keep_preview(path):
    """Dry run: keep a copy so the run's previews can be looked at."""
    if DRY_RUN:
        os.makedirs(os.path.join(HERE, "previews"), exist_ok=True)
        with open(path, "rb") as src, \
                open(os.path.join(HERE, "previews", os.path.basename(path)), "wb") as dst:
            dst.write(src.read())


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def post_date(post, cfg):
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")) \
        if post.get("created_at") else now_utc()
    return created.astimezone(tz)


def make_card_file(post, state, cfg, text=None, page=None, size=None):
    """The post as an image card; returns the JPEG's path. text, page ("2/3")
    and size (font px) make one card of a long post split over several."""
    user = state["user"]
    footer = cfg.get("card_footer") or "Follow @{} on X".format(user["username"])
    if page:
        footer = "{}  ·  {}".format(footer, page)
    path = os.path.join(WORK, "card-{}{}.jpg".format(
        post["id"], "-" + page.split("/")[0] if page else ""))
    if not os.path.exists(path):  # Reel, Story and post share the card
        render_card(post["text"] if text is None else text, path, user["name"],
                    user["username"], avatar_url=user.get("avatar"),
                    created_at=post_date(post, cfg), quote=post.get("quote"),
                    theme=cfg.get("card_theme", "dark"), footer=footer, size=size)
        keep_preview(path)
    return path


def make_card(post, state, cfg, **kw):
    return read_bytes(make_card_file(post, state, cfg, **kw))


def is_long(post):
    """Too long for one readable card (a quoted post stays on one card)."""
    return not post.get("quote") and len(post["text"]) > CARD_CHARS


def card_files(post, state, cfg):
    """The post's cards as (path, text): one, or a numbered set for a long
    post, all at the same font size."""
    if not is_long(post):
        return [(make_card_file(post, state, cfg), post["text"])]
    pages = split_for_cards(post["text"])
    user = state["user"]
    size = min(card_font_size(t, user["name"], user["username"]) for t in pages)
    return [(make_card_file(post, state, cfg, text=t,
                            page="{}/{}".format(i, len(pages)), size=size), t)
            for i, t in enumerate(pages, 1)]


def _wrap(line, limit):
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        yield line[:cut]
        line = line[cut:].lstrip()
    yield line


def split_for_cards(text, limit=CARD_CHARS, most=10):
    """Split a long post into card-sized pages, keeping paragraphs whole
    where they fit, and evening out the pages so none is nearly empty.
    Instagram carousels hold 10 at most."""
    pages = _pack(text, limit)
    target = len(text) / len(pages)
    # Pack again with the smallest page size that needs no extra pages.
    for size in range(int(target), limit):
        even = _pack(text, limit, page=size)
        if len(even) <= len(pages):
            pages = even
            break
    if len(pages) > most:
        pages = pages[:most]
        pages[-1] = pages[-1].rstrip() + " …"
    return pages


def _pack(text, limit, page=None):
    """Fill pages up to `page` characters; only paragraphs longer than
    `limit` (a full card) are broken up."""
    page = page or limit
    pages, cur = [], ""

    def add(chunk, sep):
        nonlocal cur
        if cur and len(cur) + len(sep) + len(chunk) > page:
            pages.append(cur)
            cur = chunk
        else:
            cur = cur + sep + chunk if cur else chunk

    for para in re.split(r"\n\s*\n", text.strip()):
        if len(para) <= limit:
            add(para, "\n\n")
            continue
        sep = "\n\n"
        for line in para.split("\n"):
            for piece in _wrap(line, limit):
                add(piece, sep)
                sep = "\n"
    if cur:
        pages.append(cur)
    return pages


def chart_title(text, limit=110):
    """The post's first line, without links, to head a chart frame."""
    lines = [l.strip() for l in URL_RE.sub("", text).splitlines() if l.strip()]
    title = lines[0] if lines else ""
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return title


def url_key(url):
    return hashlib.md5(url.encode()).hexdigest()[:10]


def chart_file(url):
    """Download a chart photo once per run; returns its local path."""
    path = os.path.join(WORK, "chart-{}.png".format(url_key(url)))
    if not os.path.exists(path):
        download_image(url).convert("RGB").save(path)
    return path


def frame_chart(post, url, cfg, layout="feed"):
    """The chart inside The ChartRoom frame. Returns the JPEG's path and
    where the chart sits in it (a Reel animates that area)."""
    chart = chart_file(url)
    out = os.path.join(WORK, "{}-{}-{}.jpg".format(layout, post["id"], url_key(url)))
    rect = render_chart_frame(
        chart, out, chart_title(post["text"]), cfg.get("ig_handle", ""),
        os.path.join(HERE, cfg.get("frame_logo", cfg["logo_image"])),
        post_date(post, cfg).strftime("%-d %b %Y"), layout=layout,
        theme=cfg.get("card_theme", "dark"),
        ask=cfg.get("frame_ask", "📤 Send to a trader friend"))
    keep_preview(out)
    return out, rect


def reading_seconds(text):
    """How long a card stays on screen in a Reel."""
    return min(20, max(6, len(text) / 18 + 3))


def make_reel(post, state, cfg, kind):
    """The post as a Reel, built once per run and shared by Facebook and
    Instagram: its charts one after another ("charts"), or its cards
    ("cards"), each slowly zooming in."""
    path = os.path.join(WORK, "reel-{}.mp4".format(post["id"]))
    if os.path.exists(path):
        return path
    theme = cfg.get("card_theme", "dark")
    if kind == "charts":
        photos = split_media(post)[1][:10]
        each = cfg.get("reel_seconds", 10) if len(photos) == 1 else \
            cfg.get("reel_seconds_each", 5)
        slides = []
        for p in photos:
            frame, rect = frame_chart(post, p["url"], cfg, layout="reel")
            slides.append((frame, chart_file(p["url"]), rect, each, 0.2, "right"))
    else:
        cards = card_files(post, state, cfg)
        times = [reading_seconds(t) for _, t in cards]
        squeeze = min(1, (FB_REEL_MAX - 2) / sum(times))
        background = os.path.join(WORK, "tall-background.jpg")
        card_on_tall(None, background, theme, background_only=True)
        slides = [(background, c, CARD_ON_TALL, round(s * squeeze, 1), 0.04, "center")
                  for (c, _), s in zip(cards, times)]
    parts = []
    for i, (frame, picture, rect, seconds, zoom, anchor) in enumerate(slides):
        part = os.path.join(WORK, "part-{}-{}.mp4".format(post["id"], i))
        parts.append(render_reel(frame, picture, rect, part, seconds=seconds,
                                 zoom=zoom, anchor=anchor))
    join_reels(parts, path)
    keep_preview(path)
    return path


def reel_kind(post, cfg, platform):
    """Reels reach people who don't follow the account yet, so most posts go
    out as one. Returns "charts", "cards", or None to post another way."""
    if platform not in cfg.get("chart_reels", []):
        return None
    videos, photos = split_media(post)
    if videos or uses_logo(post, cfg):
        return None  # a video already goes out as a Reel
    if photos:
        return "charts"
    if not cfg.get("text_reels"):
        return None
    if platform == "instagram" and is_long(post):
        return None  # a carousel reads better than a long run of cards
    return "cards"


def try_reel(platform, upload):
    """Build and upload a Reel. Returns None if that fails: nothing has been
    published yet, so the caller posts the chart as a photo instead."""
    try:
        return upload()
    except Exception as e:
        if is_meta_blocked(e):
            raise
        print("  {}: Reel failed ({}); posting the photo instead".format(platform, e))
        traceback.print_exc()
        return None


HASHTAG_RE = re.compile(r"(?<![\w&])#(\w+)")


def hashtags_for(text, cfg):
    """Hashtags to add, keeping the post's total within Instagram's limit
    (5): first ones matched from the post's words, then the general ones."""
    opts = cfg.get("hashtags") or {}
    have = {t.lower() for t in HASHTAG_RE.findall(text)}
    room = opts.get("max", 5) - len(have)
    matched = [tag for pattern, tag in opts.get("keywords", {}).items()
               if re.search(r"\b(?:{})\b".format(pattern), text, re.I)]
    tags = []
    for tag in matched + opts.get("base", []):
        if len(tags) >= room:
            break
        if tag.lstrip("#").lower() not in have:
            have.add(tag.lstrip("#").lower())
            tags.append(tag)
    return tags


def uses_logo(post, cfg):
    """Text posts carrying a join link (e.g. the Telegram link) go out as the
    text itself plus the logo, not as a card, so the link stays usable."""
    if post["media"]:
        return False
    return any(m in post["text"] for m in cfg.get("logo_link_match", []))


def logo_bytes(cfg, key):
    with open(os.path.join(HERE, cfg[key]), "rb") as f:
        return f.read()


def split_media(post):
    videos = [m for m in post["media"] if m["type"] == "video"]
    photos = [m for m in post["media"] if m["type"] == "photo"]
    return videos, photos


def fb_message(post, cfg):
    if uses_logo(post, cfg):
        return post["text"]
    return "\n\n".join(p for p in (
        post["text"], cfg.get("fb_footer"), cfg.get("disclaimer")) if p)


def fb_album(message, photo_ids):
    """One Page post showing several unpublished photos."""
    data = {"message": message}
    for i, pid in enumerate(photo_ids):
        data["attached_media[{}]".format(i)] = json.dumps({"media_fbid": pid})
    graph("POST", "{}/feed".format(page()), data=data)


def fb_reel_upload(post, state, cfg, kind):
    video = make_reel(post, state, cfg, kind)
    start = graph("POST", "{}/video_reels".format(page()), data={"upload_phase": "start"})
    rupload("video-upload", start["video_id"], video)
    return start["video_id"]


def post_facebook(post, state, cfg):
    msg = fb_message(post, cfg)
    videos, photos = split_media(post)
    reel, kind = None, reel_kind(post, cfg, "facebook")
    if kind:
        reel = try_reel("facebook", lambda: fb_reel_upload(post, state, cfg, kind))
    if reel:
        graph("POST", "{}/video_reels".format(page()), data={
            "upload_phase": "finish", "video_id": reel,
            "video_state": "PUBLISHED", "description": msg})
    elif videos:
        graph("POST", "{}/videos".format(page()),
              data={"file_url": videos[0]["url"], "description": msg})
    elif len(photos) == 1:
        graph("POST", "{}/photos".format(page()),
              data={"url": photos[0]["url"] + "?name=large", "message": msg})
    elif photos:
        fb_album(msg, [graph("POST", "{}/photos".format(page()), data={
            "url": p["url"] + "?name=large", "published": "false"})["id"]
            for p in photos])
    elif uses_logo(post, cfg):
        graph("POST", "{}/photos".format(page()), data={"message": msg},
              files={"source": ("logo.jpg", logo_bytes(cfg, "logo_image"), "image/jpeg")})
    elif post.get("quote"):
        # A bare text post would lose the quoted post, so show it on the card.
        graph("POST", "{}/photos".format(page()), data={"message": msg},
              files={"source": ("card.jpg", make_card(post, state, cfg), "image/jpeg")})
    else:
        graph("POST", "{}/feed".format(page()), data={"message": msg})


def ig_caption(post, state, cfg):
    logo = uses_logo(post, cfg)
    tail = []
    if logo and cfg.get("ig_link_note"):
        tail.append(cfg["ig_link_note"])  # links in IG captions aren't clickable
    tail.append(cfg.get("ig_footer", "").format(
        handle=state["user"]["username"], ig_handle=cfg.get("ig_handle", "")))
    if not logo:
        tail.append(cfg.get("disclaimer", ""))
    tail.append(" ".join(hashtags_for(post["text"], cfg)))
    tail = "\n\n".join(p for p in tail if p)
    # Over the limit, shorten the post text, never the footer or hashtags.
    text, room = post["text"], IG_CAPTION_LIMIT - len(tail) - 2
    if len(text) > room:
        text = text[:room - 1].rstrip() + "…"
    return "\n\n".join(p for p in (text, tail) if p)


def ig_image(post, url, cfg, pad):
    """A chart photo ready for Instagram: framed, or padded to fit 4:5."""
    if cfg.get("frame_charts"):
        return read_bytes(frame_chart(post, url, cfg)[0])
    return fit_for_instagram(download_image(url), pad)


def alt_text(text):
    text = " ".join(URL_RE.sub("", text).split())
    return text if len(text) <= ALT_CHARS else text[:ALT_CHARS - 1].rstrip() + "…"


def chart_alt(text):
    return alt_text("Chart: " + (chart_title(text) or "market chart"))


def ig_reel_container(post, state, cfg, kind, caption):
    video = make_reel(post, state, cfg, kind)
    data = {"media_type": "REELS", "upload_type": "resumable",
            "caption": caption, "share_to_feed": "true"}
    if cfg.get("ig_trial_reels"):  # shown to non-followers first
        data["trial_params"] = json.dumps(
            {"graduation_strategy": cfg["ig_trial_reels"]})
    container = graph("POST", "{}/media".format(ig()), data=data)
    rupload("ig-api-upload", container["id"], video)
    wait_until_ready(container["id"])
    return container


def post_instagram(post, state, cfg):
    caption = ig_caption(post, state, cfg)
    container, kind = None, reel_kind(post, cfg, "instagram")
    if kind:
        container = try_reel("instagram", lambda: ig_reel_container(
            post, state, cfg, kind, caption))
    if not container:
        container = ig_container(post, state, cfg, caption)
    wait_until_ready(container["id"])
    publish_container(container["id"])


def ig_container(post, state, cfg, caption):
    """The Instagram media container for a post (not yet published)."""
    pad = THEMES.get(cfg.get("card_theme", "dark"), THEMES["dark"])["bg"]
    videos, photos = split_media(post)
    if videos:
        container = graph("POST", "{}/media".format(ig()), data={
            "media_type": "REELS", "video_url": videos[0]["url"],
            "caption": caption, "share_to_feed": "true"})
    elif photos:
        urls = [host_image(ig_image(post, p["url"], cfg, pad)) for p in photos[:10]]
        container = ig_images_container(urls, caption, [chart_alt(post["text"])] * len(urls))
    elif uses_logo(post, cfg):
        container = ig_images_container(
            [host_image(logo_bytes(cfg, "logo_image_square"))], caption,
            ["The ChartRoom logo"])
    else:
        # A long post shrinks to an unreadable size (or gets cut off) on one
        # card, so it becomes a carousel of cards.
        cards = card_files(post, state, cfg)
        container = ig_images_container([host_image(read_bytes(c)) for c, _ in cards],
                                        caption, [alt_text(t) for _, t in cards])
    return container


def ig_image_item(data, alt):
    """An image container with alt text, or without it if Instagram refuses."""
    if alt:
        try:
            return graph("POST", "{}/media".format(ig()), data=dict(data, alt_text=alt))
        except RuntimeError as e:
            if is_meta_blocked(e) or "alt" not in str(e).lower():
                raise
            print("  instagram: alt text refused ({}); posting without it".format(e))
    return graph("POST", "{}/media".format(ig()), data=data)


def ig_images_container(urls, caption, alts=None):
    """One image, or a carousel of up to 10."""
    alts = alts or [None] * len(urls)
    if len(urls) == 1:
        return ig_image_item({"image_url": urls[0], "caption": caption}, alts[0])
    children = []
    for u, alt in zip(urls, alts):
        child = ig_image_item({"image_url": u, "is_carousel_item": "true"}, alt)
        wait_until_ready(child["id"])
        children.append(child["id"])
    return graph("POST", "{}/media".format(ig()), data={
        "media_type": "CAROUSEL", "children": ",".join(children),
        "caption": caption})


def publish_container(container_id, tries=5, pause=15):
    """media_publish, retried: Instagram sometimes reports FINISHED a little
    before the media can be published ("Media ID is not available")."""
    for attempt in range(1, tries + 1):
        try:
            return graph("POST", "{}/media_publish".format(ig()),
                         data={"creation_id": container_id})
        except RuntimeError as e:
            if "not available" not in str(e) or attempt == tries:
                raise
            print("  instagram: media not ready yet, retrying in {}s".format(pause))
            time.sleep(pause)
            wait_until_ready(container_id)


def story_image(post, state, cfg):
    """A 1080x1920 picture of the post for Stories, or None (videos and logo
    posts don't get a Story)."""
    videos, photos = split_media(post)
    if videos or uses_logo(post, cfg):
        return None
    if photos:
        return frame_chart(post, photos[0]["url"], cfg, layout="reel")[0]
    out = os.path.join(WORK, "story-{}.jpg".format(post["id"]))
    card_on_tall(card_files(post, state, cfg)[0][0], out, cfg.get("card_theme", "dark"))
    keep_preview(out)
    return out


def ig_posts_left():
    """Instagram allows 50 API posts a day, Stories included."""
    info = (graph("GET", "{}/content_publishing_limit".format(ig()), params={
        "fields": "quota_usage,config"}).get("data") or [{}])[0]
    return info.get("config", {}).get("quota_total", 50) - info.get("quota_usage", 0)


def story(platform, post, state, cfg, share):
    """Also show the post in Stories, which keeps followers coming back. A
    Story is a bonus: if it fails, the run moves on and doesn't retry."""
    if platform not in cfg.get("stories", []):
        return
    try:
        image = story_image(post, state, cfg)
        if image:
            share(read_bytes(image))
    except Exception as e:
        if is_meta_blocked(e):
            raise
        print("  {} story skipped: {}".format(platform, e))


def ig_story(jpeg):
    if ig_posts_left() < 10:  # keep the rest of the day's quota for posts
        print("  instagram story skipped: near the daily posting limit")
        return
    container = graph("POST", "{}/media".format(ig()), data={
        "media_type": "STORIES", "image_url": host_image(jpeg)})
    wait_until_ready(container["id"])
    publish_container(container["id"])


def fb_story(jpeg):
    graph("POST", "{}/photo_stories".format(page()), data={"photo_id": fb_photo(jpeg)})


def post_instagram_story(post, state, cfg):
    story("instagram", post, state, cfg, ig_story)


def post_facebook_story(post, state, cfg):
    story("facebook", post, state, cfg, fb_story)


PLATFORMS = [("facebook", post_facebook), ("instagram", post_instagram),
             ("instagram_story", post_instagram_story),
             ("facebook_story", post_facebook_story)]

# Meta refusing the token itself (app blocked, token revoked or expired). Not
# the post's fault, so such posts wait instead of using up their attempts.
META_BLOCKED = ("Cannot call API for app", "API access blocked",
                "Error validating access token", "session has been invalidated",
                "has not authorized application")


def is_meta_blocked(error):
    return any(s in str(error) for s in META_BLOCKED)


def meta_token_warning(state):
    """Once a day, check when Meta's 90-day data access for the Page token
    lapses. Returns a warning when it is under 10 days away."""
    today = now_utc().date().isoformat()
    if state.get("token_checked") == today:
        return None
    state["token_checked"] = today
    token = os.environ["FB_PAGE_TOKEN"]
    try:
        info = graph("GET", "debug_token", params={"input_token": token}).get("data", {})
    except RuntimeError as e:
        return "Meta token check failed: {}".format(e)
    if info.get("type") not in (None, "PAGE"):
        return ("FB_PAGE_TOKEN is a {} token. It must be the Page token "
                "(see README).".format(info["type"]))
    expires = info.get("data_access_expires_at") or 0
    if expires and expires - time.time() < 10 * 86400:
        when = datetime.fromtimestamp(expires, timezone.utc).strftime("%d %b %Y")
        return "Meta token access ends {}. Renew it (see README).".format(when)
    return None


# ---------------------------------------------------------------- weekly recap

def week_posts(state):
    """The last 7 days of posts (newest 100), each with a score from its
    engagement on X, where the audience is already large."""
    params = dict(POST_FIELDS, exclude="replies,retweets", max_results=100,
                  start_time=(now_utc() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    params["tweet.fields"] += ",public_metrics"
    data = x_get("/users/{}/tweets".format(state["user"]["id"]), params)
    includes = data.get("includes", {})
    posts = []
    for p in data.get("data", []):
        post = normalize(p, includes)
        m = p.get("public_metrics", {})
        post["score"] = (m.get("like_count", 0) + m.get("reply_count", 0) + 2 * (
            m.get("retweet_count", 0) + m.get("quote_count", 0)
            + m.get("bookmark_count", 0)))
        posts.append(post)
    return posts


def build_recap(state, cfg):
    """The week's best chart posts as one carousel: a cover listing them,
    then each chart in its frame. None if the week had too few charts."""
    opts = cfg.get("recap") or {}
    charts = [p for p in week_posts(state)
              if split_media(p)[1] and not split_media(p)[0]]
    if len(charts) < opts.get("min_charts", 3):
        return None
    best = sorted(charts, key=lambda p: p["score"], reverse=True)[:opts.get("charts", 9)]
    best.sort(key=lambda p: int(p["id"]))
    titles = [chart_title(p["text"], 90) or "Chart, " + post_date(p, cfg).strftime("%-d %b")
              for p in best]
    end = now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC")))
    span = "{} – {}".format((end - timedelta(days=6)).strftime("%-d %b"),
                            end.strftime("%-d %b %Y"))
    cover = os.path.join(WORK, "recap-cover.jpg")
    render_recap_cover(titles, span, cfg.get("ig_handle", ""),
                       os.path.join(HERE, cfg.get("frame_logo", cfg["logo_image"])),
                       cover, theme=cfg.get("card_theme", "dark"))
    keep_preview(cover)
    images = [cover] + [frame_chart(p, split_media(p)[1][0]["url"], cfg)[0] for p in best]
    head = "📊 This week's top charts ({})\n\n{}".format(span, "\n".join(
        "{}. {}".format(i, t) for i, t in enumerate(titles, 1)))
    caption = "\n\n".join(p for p in (
        head, "Swipe through 👉", cfg.get("ig_footer", "").format(
            handle=state["user"]["username"], ig_handle=cfg.get("ig_handle", "")),
        cfg.get("disclaimer"), " ".join(hashtags_for(" ".join(titles), cfg))) if p)
    message = "\n\n".join(p for p in (head, cfg.get("fb_footer"), cfg.get("disclaimer")) if p)
    alts = [alt_text("This week's top charts: " + "; ".join(titles))] + \
        [alt_text("Chart: " + t) for t in titles]
    return {"images": images, "caption": caption, "message": message, "alts": alts}


def recap_instagram(recap):
    container = ig_images_container(
        [host_image(read_bytes(i)) for i in recap["images"]], recap["caption"],
        recap["alts"])
    wait_until_ready(container["id"])
    publish_container(container["id"])


def recap_facebook(recap):
    fb_album(recap["message"], [fb_photo(read_bytes(i)) for i in recap["images"]])


def recap_week(cfg):
    """This week's label ("2026-W44") once the recap is due, else None."""
    opts = cfg.get("recap") or {}
    now = now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC")))
    if not opts.get("enabled") or now.weekday() != opts.get("weekday", 6) \
            or now.hour < opts.get("hour", 11):
        return None
    return now.strftime("%G-W%V")


def weekly_recap(state, cfg, week):
    """Post the weekly recap once. A failed platform is retried on the next
    runs; after 3 tries it gives up for the week. Returns 1 when it gives up
    (so GitHub emails), else 0."""
    r = state.get("recap") or {}
    if r.get("week") != week:
        r = {"week": week, "done": {}, "tries": 0}
    state["recap"] = r
    if r.get("finished"):
        return 0
    print("Weekly recap {}".format(week))
    recap = build_recap(state, cfg)
    if not recap:
        print("  too few charts this week; no recap")
        r["finished"] = True
        save_state(state)
        return 0
    failed = False
    for name, fn in (("facebook", recap_facebook), ("instagram", recap_instagram)):
        if r["done"].get(name):
            continue
        try:
            fn(recap)
            r["done"][name] = True
            print("  recap {}: posted".format(name))
        except Exception as e:
            print("  recap {}: FAILED: {}".format(name, e))
            if is_meta_blocked(e):
                save_state(state)
                return 0  # the token warning covers this; try again later
            traceback.print_exc()
            failed = True
        save_state(state)
    r["tries"] += failed
    r["finished"] = not failed or r["tries"] >= 3
    save_state(state)
    return int(failed and r["finished"])


# ---------------------------------------------------------------- main

def preview_latest(state, cfg, count=5):
    """Dry run: show what would be posted for the latest few posts."""
    params = dict(POST_FIELDS, exclude="replies,retweets", max_results=count)
    data = x_get("/users/{}/tweets".format(state["user"]["id"]), params)
    includes = data.get("includes", {})
    posts = [normalize(p, includes) for p in data.get("data", [])]
    print("Dry run: previewing your latest {} posts. Nothing is posted.".format(len(posts)))
    failures = 0
    for post in posts:
        kinds = [m["type"] for m in post["media"]] or ["text"]
        if post.get("quote"):
            kinds.append("quote of @" + post["quote"]["handle"])
        print("\nPost {} ({}): {!r}".format(post["id"], ", ".join(kinds), post["text"][:120]))
        print("  Instagram caption ends: {!r}".format(ig_caption(post, state, cfg)[-220:]))
        entry = {"post": post, "done": {}, "attempts": 0}
        failures += publish(entry, state, cfg) + bool(entry.get("meta_blocked"))
    return failures


def publish(entry, state, cfg):
    """Publish to each platform not yet done. Returns the number of failures;
    failures caused by Meta blocking the token only set entry["meta_blocked"]."""
    failures = 0
    post = entry["post"]
    for name, fn in PLATFORMS:
        if entry["done"].get(name):
            continue
        try:
            fn(post, state, cfg)
            entry["done"][name] = True
            print("  {}: posted".format(name))
        except Exception as e:
            entry["last_error"] = "{}: {}".format(name, e)
            print("  {}: FAILED: {}".format(name, e))
            if is_meta_blocked(e):
                entry["meta_blocked"] = True
            else:
                failures += 1
                traceback.print_exc()
        save_state(state)  # after every platform, so a crash never double-posts
    return failures


def main():
    missing = [s for s in SECRETS if not os.environ.get(s)]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))

    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    state.setdefault("pending", {})
    state.setdefault("failed", [])

    if not state.get("user") or now_utc() - datetime.fromisoformat(
            state["user"]["checked"]) > timedelta(days=7):
        refresh_profile(state)

    if DRY_RUN and os.environ.get("RECAP"):
        print("Dry run: previewing this week's recap. Nothing is posted.")
        sys.exit(weekly_recap(state, cfg, "preview"))

    if DRY_RUN and not os.environ.get("TEST_POST_ID"):
        sys.exit(1 if preview_latest(state, cfg) else 0)

    if os.environ.get("TEST_POST_ID"):
        post = fetch_one_post(os.environ["TEST_POST_ID"])
        print("Test post {}: {!r}".format(post["id"], post["text"][:80]))
        entry = {"post": post, "done": {}, "attempts": 0}
        failures = publish(entry, state, cfg) + bool(entry.get("meta_blocked"))
        state.pop("token_checked", None)  # always check the token on a test
        warning = meta_token_warning(state)
        if warning:
            print("WARNING: " + warning)
        save_state(state)
        sys.exit(1 if failures or warning else 0)

    if not state.get("started_at"):
        state["started_at"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state(state)
        print("First run for @{}: mirroring posts made from {} onwards.".format(
            state["user"]["username"], state["started_at"]))
        return

    for post in fetch_new_posts(state):
        print("New post {}: {!r}".format(post["id"], post["text"][:80]))
        state["pending"][post["id"]] = {"post": post, "done": {}, "attempts": 0}
    save_state(state)

    failures = 0
    meta_blocked = False
    for pid in sorted(state["pending"], key=int):
        entry = state["pending"][pid]
        print("Publishing {}".format(pid))
        # A post failing for the first time is retried quietly on the next run;
        # only a second failure in a row counts towards the email.
        if publish(entry, state, cfg) and entry["attempts"] >= 1:
            failures += 1
        if all(entry["done"].get(n) for n, _ in PLATFORMS):
            del state["pending"][pid]
            continue
        if entry.pop("meta_blocked", False):
            meta_blocked = True  # keep the post waiting until access is fixed
            save_state(state)
            continue
        entry["attempts"] += 1
        if entry["attempts"] >= MAX_ATTEMPTS:
            print("  giving up on {} after {} attempts".format(pid, MAX_ATTEMPTS))
            state["failed"] = (state["failed"] + [entry])[-20:]
            del state["pending"][pid]
        save_state(state)

    week = recap_week(cfg)
    if week and not meta_blocked:
        failures += weekly_recap(state, cfg, week)

    if not state["pending"] and failures == 0:
        print("Up to date.")
    warning = meta_token_warning(state)
    if meta_blocked:
        print("WARNING: Meta is refusing the Page token; {} post(s) waiting "
              "(see README).".format(len(state["pending"])))
        today = now_utc().date().isoformat()
        if state.get("meta_alert_day") != today:  # one email a day, not every run
            state["meta_alert_day"] = today
            failures += 1
    save_state(state)
    if warning:
        print("WARNING: " + warning)
    if failures or warning:
        sys.exit(1)  # makes GitHub email you about the failed run


if __name__ == "__main__":
    main()
