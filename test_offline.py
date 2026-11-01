"""Offline check of mirror.py: fake X responses, record Meta calls.

Run: python test_offline.py   (needs Chrome and imageio-ffmpeg for the
card, frame and Reel renders)
"""

import os
import tempfile

from PIL import Image

import mirror

for s in mirror.SECRETS:
    os.environ[s] = "test"
os.environ["FB_PAGE_ID"], os.environ["IG_USER_ID"] = "PAGE", "IG"

tmp = tempfile.mkdtemp()
mirror.STATE_PATH = os.path.join(tmp, "state.json")

USER = {"id": "42", "name": "Test User", "username": "testuser",
        "profile_image_url": "https://example.com/a_normal.jpg"}

TIMELINE = {
    "data": [  # newest first, as X returns them
        {"id": "103", "text": "Look at this chart https://t.co/pic",
         "created_at": "2026-10-25T05:00:00.000Z",
         "attachments": {"media_keys": ["3_1"]},
         "entities": {"urls": [{"url": "https://t.co/pic", "media_key": "3_1",
                                "expanded_url": "https://x.com/testuser/status/103/photo/1"}]}},
        {"id": "102", "text": "Agree with this &amp; more https://t.co/q",
         "created_at": "2026-10-25T04:00:00.000Z",
         "referenced_tweets": [{"type": "quoted", "id": "900"}],
         "entities": {"urls": [{"url": "https://t.co/q",
                                "expanded_url": "https://x.com/other/status/900"}]}},
        {"id": "101", "text": "Plain thought. Read https://t.co/abc",
         "created_at": "2026-10-25T03:00:00.000Z",
         "entities": {"urls": [{"url": "https://t.co/abc",
                                "expanded_url": "https://example.com/article"}]}},
    ],
    "includes": {
        "media": [{"media_key": "3_1", "type": "photo",
                   "url": "https://pbs.twimg.com/media/abc.jpg"}],
        "tweets": [{"id": "900", "text": "Quoted wisdom", "author_id": "7"}],
        "users": [{"id": "7", "name": "Other Person", "username": "other"}],
    },
    "meta": {"newest_id": "103"},
}

x_calls, meta_calls = [], []
timeline_queue = []


def fake_x_get(path, params=None):
    x_calls.append((path, dict(params or {})))
    if path == "/users/me":
        return {"data": USER}
    return timeline_queue.pop(0) if timeline_queue else {"meta": {"result_count": 0}}


fail_instagram_once = {"on": False}


def fake_graph(method, path, data=None, files=None, params=None):
    meta_calls.append((method, path, dict(data or {}), bool(files)))
    if fail_instagram_once["on"] and path == "IG/media_publish":
        fail_instagram_once["on"] = False
        raise RuntimeError("simulated Instagram outage")
    return {"id": "m{}".format(len(meta_calls)), "video_id": "v{}".format(len(meta_calls)),
            "status_code": "FINISHED",
            "images": [{"width": 720, "source": "https://cdn.example/s.jpg"},
                       {"width": 1440, "source": "https://cdn.example/l.jpg"}]}


uploads = []
reel_upload_fails = {"on": False}


def fake_rupload(kind, media_id, video_path):
    if reel_upload_fails["on"]:
        raise RuntimeError("Meta upload 500 on {}: simulated".format(kind))
    assert os.path.getsize(video_path) > 10000, "Reel video should exist"
    uploads.append((kind, media_id))


mirror.x_get = fake_x_get
mirror.graph = fake_graph
mirror.rupload = fake_rupload
DISCLAIMER = mirror.load_json(mirror.CONFIG_PATH, {})["disclaimer"]
mirror.download_image = lambda url: Image.new("RGB", (900, 1600), "white")  # tall screenshot


def run():
    try:
        mirror.main()
        return 0
    except SystemExit as e:
        return e.code or 0


# 1. First run only records the start time.
assert run() == 0
state = mirror.load_json(mirror.STATE_PATH, {})
assert state["started_at"] and not meta_calls, "first run must not post"
print("ok  first run posts nothing, starts from", state["started_at"])

# 2. Three new posts arrive; Instagram fails once on the first publish.
timeline_queue.append(TIMELINE)
fail_instagram_once["on"] = True
assert run() == 0, "a first failure is retried quietly, no email"
state = mirror.load_json(mirror.STATE_PATH, {})
timeline_params = x_calls[-1][1]
assert timeline_params["exclude"] == "replies,retweets"
assert "start_time" in timeline_params and "since_id" not in timeline_params
assert state["since_id"] == "103"
assert list(state["pending"]) == ["101"]
assert state["pending"]["101"]["done"] == {"facebook": True}
print("ok  failed Instagram post kept for retry (no email yet), Facebook not repeated")

fb_feed = [c for c in meta_calls if c[1] == "PAGE/feed"]
assert fb_feed[0][2]["message"] == "Plain thought. Read https://example.com/article\n\n" + DISCLAIMER
fb_photos = [c for c in meta_calls if c[1] == "PAGE/photos" and c[2].get("message")]
assert fb_photos[0][2]["message"].startswith("Agree with this & more\n\n") and fb_photos[0][3]
ig_media = [c for c in meta_calls if c[1] == "IG/media"]
ig_images = [c for c in ig_media if "image_url" in c[2]]
assert all(c[2]["image_url"] == "https://cdn.example/l.jpg" for c in ig_images)
caption = ig_images[0][2]["caption"]
assert "Follow @dr.romeetsaha" in caption and DISCLAIMER in caption
assert "Follow me on X" not in caption
assert caption.endswith("#stockmarketindia #technicalanalysis #swingtrading #nse"), caption
print("ok  links expanded, media links dropped, quote card, IG caption footer + hashtags")

# The chart photo (103) went out as a Reel on both, not as a photo.
fb_reels = [c for c in meta_calls if c[1] == "PAGE/video_reels"]
assert [c[2]["upload_phase"] for c in fb_reels] == ["start", "finish"]
assert fb_reels[1][2]["description"].startswith("Look at this chart")
assert not any(c[2].get("url", "").startswith("https://pbs.twimg.com") for c in meta_calls)
ig_reel = [c for c in ig_media if c[2].get("media_type") == "REELS"][0][2]
assert ig_reel["upload_type"] == "resumable" and ig_reel["share_to_feed"] == "true"
assert [u[0] for u in uploads] == ["video-upload", "ig-api-upload"]
print("ok  chart photo became a Reel on Facebook and Instagram (one video, two uploads)")

# 3. Next run: no new posts, retries only Instagram for 101.
before = len(meta_calls)
assert run() == 0
state = mirror.load_json(mirror.STATE_PATH, {})
assert state["pending"] == {}
assert "since_id" in x_calls[-1][1]
assert not any(c[1].startswith("PAGE/feed") for c in meta_calls[before:])
print("ok  retry posts only to Instagram, then pending is empty")

# 4. Tall image is padded to Instagram's 4:5 limit.
jpeg = mirror.fit_for_instagram(Image.new("RGB", (900, 1600)), "#000000")
import io
w, h = Image.open(io.BytesIO(jpeg)).size
assert abs(w / h - 0.8) < 0.01, (w, h)
print("ok  tall image padded to 4:5 ({}x{})".format(w, h))

# 5. Card render through the real pipeline.
post = mirror.normalize(TIMELINE["data"][1], TIMELINE["includes"])
data = mirror.make_card(post, {"user": {"name": "Test User", "username": "testuser"}},
                        {"timezone": "Asia/Kolkata"})
assert Image.open(io.BytesIO(data)).size == (1080, 1350)
print("ok  card renders 1080x1350")

# 6. A text post with the Telegram join link goes out as text + logo, no card.
cfg = mirror.load_json(mirror.CONFIG_PATH, {})
link_post = {"id": "200", "created_at": None, "media": [], "quote": None,
             "text": "Join telegram here: https://cosmofeed.com/vig/683f9c20"}
assert mirror.uses_logo(link_post, cfg)
assert not mirror.uses_logo(dict(link_post, text="no link here"), cfg)
assert not mirror.uses_logo(dict(link_post, media=[{"type": "photo", "url": "u"}]), cfg)
mirror.make_card = lambda *a: (_ for _ in ()).throw(AssertionError("card used"))
before = len(meta_calls)
mirror.publish({"post": link_post, "done": {}, "attempts": 0},
               {"user": {"username": "testuser"}}, cfg)
calls = meta_calls[before:]
fb = calls[0]
assert fb[1] == "PAGE/photos" and fb[2]["message"] == link_post["text"] and fb[3]
ig_caption = [c for c in calls if c[1] == "IG/media"][0][2]["caption"]
assert "https://cosmofeed.com/vig/683f9c20" in ig_caption
print("ok  join-link post: full text + logo on FB and IG, no card")

# 7. Meta blocking the token: the post waits (no attempts used), one email a day.
def blocked_graph(method, path, data=None, files=None, params=None):
    raise RuntimeError("Meta API 400 on x: Cannot call API for app 1 on behalf of user 2")
mirror.graph = blocked_graph
state = {"user": {"id": "42", "name": "Test User", "username": "testuser",
                  "checked": mirror.now_utc().isoformat()},
         "started_at": "2026-10-25T00:00:00Z", "since_id": "103", "failed": [],
         "pending": {"300": {"post": dict(link_post, id="300"), "done": {}, "attempts": 0}},
         "token_checked": "2000-01-01"}
mirror.save_state(state)
assert run() == 1, "first blocked run of the day should alert"
state = mirror.load_json(mirror.STATE_PATH, {})
assert state["pending"]["300"]["attempts"] == 0
assert run() == 0, "later blocked runs the same day stay quiet"
state = mirror.load_json(mirror.STATE_PATH, {})
assert "300" in state["pending"] and state["pending"]["300"]["attempts"] == 0
print("ok  Meta block: post kept waiting, attempts unused, one alert a day")

# 8. "Media ID is not available" is retried inside the same run.
mirror.graph = fake_graph
not_ready = {"left": 2}
def flaky_graph(method, path, data=None, files=None, params=None):
    if path == "IG/media_publish" and not_ready["left"]:
        not_ready["left"] -= 1
        raise RuntimeError("Meta API 400 on IG/media_publish: Media ID is not available")
    return fake_graph(method, path, data, files, params)
mirror.graph = flaky_graph
mirror.time.sleep = lambda s: None
mirror.publish_container("c1")
assert not_ready["left"] == 0
print("ok  'Media ID is not available' retried in the same run")

# 9. A post failing on two runs in a row emails; the first failure doesn't.
def ig_down(method, path, data=None, files=None, params=None):
    if path == "IG/media_publish":
        raise RuntimeError("simulated Instagram outage")
    return fake_graph(method, path, data, files, params)
mirror.graph = ig_down
state = mirror.load_json(mirror.STATE_PATH, {})
state["pending"] = {"400": {"post": dict(link_post, id="400"), "done": {}, "attempts": 0}}
state["meta_alert_day"] = None
mirror.save_state(state)
assert run() == 0, "first failure: quiet"
assert run() == 1, "second failure in a row: email"
state = mirror.load_json(mirror.STATE_PATH, {})
assert state["pending"]["400"]["done"] == {"facebook": True}
assert state["pending"]["400"]["attempts"] == 2
print("ok  one-off failure is quiet, repeated failure emails")

# 10. A Reel that fails to upload falls back to the photo, framed on Instagram.
mirror.graph = fake_graph
reel_upload_fails["on"] = True
chart_post = mirror.normalize(TIMELINE["data"][0], TIMELINE["includes"])
chart_post["id"] = "500"
before = len(meta_calls)
entry = {"post": chart_post, "done": {}, "attempts": 0}
assert mirror.publish(entry, state, cfg) == 0
calls = meta_calls[before:]
assert entry["done"] == {"facebook": True, "instagram": True}
assert any(c[1] == "PAGE/photos" and c[2].get("message", "").startswith("Look at this chart")
           for c in calls)
assert not any(c[2].get("upload_phase") == "finish" for c in calls)
assert any(c[1] == "IG/media" and "image_url" in c[2] for c in calls)
assert any(c[1] == "IG/media_publish" for c in calls)
reel_upload_fails["on"] = False
print("ok  failed Reel upload falls back to the photo on both platforms")

# 11. Hashtags stay within 5 in all, counting the post's own.
cfg = mirror.load_json(mirror.CONFIG_PATH, {})
tags = mirror.hashtags_for("#Nifty looks weak, bank nifty too", cfg)
assert tags[:2] == ["#banknifty", "#nifty50"] and len(tags) == 4, tags
assert mirror.hashtags_for("#a #b #c #d #e", cfg) == []
assert "#nifty50" not in mirror.hashtags_for("#banknifty breakout", cfg)
print("ok  hashtags matched from the text, 5 at most")

# 12. Frame title: first line, no links, shortened at a word.
assert mirror.chart_title("#Nifty breakout https://t.co/x\nmore detail") == "#Nifty breakout"
long = mirror.chart_title("word " * 40)
assert long.endswith("…") and len(long) <= 111
print("ok  chart frame title")

# 13. A long text post becomes a carousel of cards, paragraphs kept whole.
paras = ["Company {}\nRevenue up {}% and the order book keeps growing nicely.".format(i, i * 7)
         for i in range(14)]
long_text = "Q2 FY27: Concalls with Strong Growth Guidance\n\n" + "\n\n".join(paras)
pages = mirror.split_for_cards(long_text)
assert len(pages) > 1 and all(len(p) <= mirror.CARD_CHARS for p in pages)
assert "\n\n".join(pages) == long_text, "nothing lost or reordered"
assert pages[0].startswith("Q2 FY27")
assert min(map(len, pages)) > 0.5 * max(map(len, pages)), "pages evened out"
assert all(p.split("\n\n")[0].startswith(("Q2", "Company")) for p in pages), "paragraphs kept whole"
assert all(len(p) <= mirror.CARD_CHARS for p in mirror.split_for_cards("x" * 2000 + " end"))
long_post = {"id": "600", "created_at": None, "media": [], "quote": None, "text": long_text}
rendered = []
mirror.make_card = lambda post, state, cfg, text=None, page=None, size=None: \
    rendered.append(page) or b"jpg"
mirror.card_font_size = lambda text, name, handle: 40
before = len(meta_calls)
mirror.publish({"post": long_post, "done": {"facebook": True}, "attempts": 0},
               {"user": {"username": "testuser", "name": "Test User"}}, cfg)
calls = meta_calls[before:]
carousel = [c for c in calls if c[2].get("media_type") == "CAROUSEL"]
assert len(carousel) == 1 and len(carousel[0][2]["children"].split(",")) == len(pages)
assert rendered == ["{}/{}".format(i, len(pages)) for i in range(1, len(pages) + 1)]
print("ok  long text post: carousel of {} cards".format(len(pages)))
print("ALL TESTS PASSED")
