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

import html
import io
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image
from requests_oauthlib import OAuth1

from card import THEMES, render_card

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
CONFIG_PATH = os.path.join(HERE, "config.json")

X_API = "https://api.x.com/2"
MAX_ATTEMPTS = 5
IG_CAPTION_LIMIT = 2200
DRY_RUN = os.environ.get("DRY_RUN") == "1"

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
        return {"id": "dry-run", "status_code": "FINISHED",
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


def host_image(jpeg):
    """Instagram fetches images from a public URL. Upload the image to the
    Page as an unpublished photo (never shown on the Page) and use its URL."""
    photo = graph("POST", "{}/photos".format(page()), data={"published": "false"},
                  files={"source": ("image.jpg", jpeg, "image/jpeg")})
    info = graph("GET", photo["id"], params={"fields": "images"})
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


def make_card(post, state, cfg):
    user = state["user"]
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")) \
        if post.get("created_at") else now_utc()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "card.jpg")
        render_card(post["text"], path, user["name"], user["username"],
                    avatar_url=user.get("avatar"), created_at=created.astimezone(tz),
                    quote=post.get("quote"), theme=cfg.get("card_theme", "dark"))
        with open(path, "rb") as f:
            data = f.read()
    if DRY_RUN:  # keep a copy so the run's previews can be looked at
        os.makedirs(os.path.join(HERE, "previews"), exist_ok=True)
        with open(os.path.join(HERE, "previews", "card-{}.jpg".format(post["id"])), "wb") as f:
            f.write(data)
    return data


def split_media(post):
    videos = [m for m in post["media"] if m["type"] == "video"]
    photos = [m for m in post["media"] if m["type"] == "photo"]
    return videos, photos


def post_facebook(post, state, cfg):
    msg = post["text"]
    videos, photos = split_media(post)
    if videos:
        graph("POST", "{}/videos".format(page()),
              data={"file_url": videos[0]["url"], "description": msg})
    elif len(photos) == 1:
        graph("POST", "{}/photos".format(page()),
              data={"url": photos[0]["url"] + "?name=large", "message": msg})
    elif photos:
        data = {"message": msg}
        for i, p in enumerate(photos):
            photo = graph("POST", "{}/photos".format(page()),
                          data={"url": p["url"] + "?name=large", "published": "false"})
            data["attached_media[{}]".format(i)] = json.dumps({"media_fbid": photo["id"]})
        graph("POST", "{}/feed".format(page()), data=data)
    elif post.get("quote"):
        # A bare text post would lose the quoted post, so show it on the card.
        graph("POST", "{}/photos".format(page()), data={"message": msg},
              files={"source": ("card.jpg", make_card(post, state, cfg), "image/jpeg")})
    else:
        graph("POST", "{}/feed".format(page()), data={"message": msg})


def ig_caption(post, state, cfg):
    parts = [post["text"]]
    footer = cfg.get("ig_footer", "").format(handle=state["user"]["username"])
    if footer:
        parts.append(footer)
    if cfg.get("ig_hashtags"):
        parts.append(cfg["ig_hashtags"])
    caption = "\n\n".join(p for p in parts if p)
    return caption[:IG_CAPTION_LIMIT]


def post_instagram(post, state, cfg):
    caption = ig_caption(post, state, cfg)
    pad = THEMES.get(cfg.get("card_theme", "dark"), THEMES["dark"])["bg"]
    videos, photos = split_media(post)
    if videos:
        container = graph("POST", "{}/media".format(ig()), data={
            "media_type": "REELS", "video_url": videos[0]["url"],
            "caption": caption, "share_to_feed": "true"})
    elif photos:
        urls = [host_image(fit_for_instagram(download_image(p["url"]), pad))
                for p in photos[:10]]
        if len(urls) == 1:
            container = graph("POST", "{}/media".format(ig()),
                              data={"image_url": urls[0], "caption": caption})
        else:
            children = []
            for u in urls:
                child = graph("POST", "{}/media".format(ig()),
                              data={"image_url": u, "is_carousel_item": "true"})
                wait_until_ready(child["id"])
                children.append(child["id"])
            container = graph("POST", "{}/media".format(ig()), data={
                "media_type": "CAROUSEL", "children": ",".join(children),
                "caption": caption})
    else:
        url = host_image(make_card(post, state, cfg))
        container = graph("POST", "{}/media".format(ig()),
                          data={"image_url": url, "caption": caption})
    wait_until_ready(container["id"])
    graph("POST", "{}/media_publish".format(ig()), data={"creation_id": container["id"]})


PLATFORMS = [("facebook", post_facebook), ("instagram", post_instagram)]


def meta_token_warning(state):
    """Once a day, check when Meta's 90-day data access for the Page token
    lapses. Returns a warning when it is under 10 days away."""
    today = now_utc().date().isoformat()
    if state.get("token_checked") == today:
        return None
    state["token_checked"] = today
    token = os.environ["FB_PAGE_TOKEN"]
    info = graph("GET", "debug_token", params={"input_token": token}).get("data", {})
    if info.get("type") not in (None, "PAGE"):
        return ("FB_PAGE_TOKEN is a {} token. It must be the Page token "
                "(see README).".format(info["type"]))
    expires = info.get("data_access_expires_at") or 0
    if expires and expires - time.time() < 10 * 86400:
        when = datetime.fromtimestamp(expires, timezone.utc).strftime("%d %b %Y")
        return "Meta token access ends {}. Renew it (see README).".format(when)
    return None


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
        print("  Instagram caption: {!r}".format(ig_caption(post, state, cfg)[-120:]))
        failures += publish({"post": post, "done": {}, "attempts": 0}, state, cfg)
    return failures


def publish(entry, state, cfg):
    """Publish to each platform not yet done. Returns the number of failures."""
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
            failures += 1
            entry["last_error"] = "{}: {}".format(name, e)
            print("  {}: FAILED: {}".format(name, e))
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

    if DRY_RUN and not os.environ.get("TEST_POST_ID"):
        sys.exit(1 if preview_latest(state, cfg) else 0)

    if os.environ.get("TEST_POST_ID"):
        post = fetch_one_post(os.environ["TEST_POST_ID"])
        print("Test post {}: {!r}".format(post["id"], post["text"][:80]))
        entry = {"post": post, "done": {}, "attempts": 0}
        failures = publish(entry, state, cfg)
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
    for pid in sorted(state["pending"], key=int):
        entry = state["pending"][pid]
        print("Publishing {}".format(pid))
        failures += publish(entry, state, cfg)
        if all(entry["done"].get(n) for n, _ in PLATFORMS):
            del state["pending"][pid]
            continue
        entry["attempts"] += 1
        if entry["attempts"] >= MAX_ATTEMPTS:
            print("  giving up on {} after {} attempts".format(pid, MAX_ATTEMPTS))
            state["failed"] = (state["failed"] + [entry])[-20:]
            del state["pending"][pid]
        save_state(state)

    if not state["pending"] and failures == 0:
        print("Up to date.")
    warning = meta_token_warning(state)
    save_state(state)
    if warning:
        print("WARNING: " + warning)
    if failures or warning:
        sys.exit(1)  # makes GitHub email you about the failed run


if __name__ == "__main__":
    main()
