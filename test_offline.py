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
assert state["pending"]["101"]["done"] == {
    "facebook": True, "instagram_story": True, "facebook_story": True}
print("ok  failed Instagram post kept for retry (no email yet), Facebook not repeated")

# All three posts (text, quote, chart) went out as Reels on both.
FB_FOOTER = mirror.load_json(mirror.CONFIG_PATH, {})["fb_footer"]
finishes = [c[2]["description"] for c in meta_calls
            if c[1] == "PAGE/video_reels" and c[2]["upload_phase"] == "finish"]
assert finishes[0] == "Plain thought. Read https://example.com/article\n\n{}\n\n{}".format(
    FB_FOOTER, DISCLAIMER)
assert finishes[1].startswith("Agree with this & more\n\n")
assert finishes[2].startswith("Look at this chart")
assert not any(c[1] == "PAGE/feed" for c in meta_calls)
assert not any(c[2].get("url", "").startswith("https://pbs.twimg.com") for c in meta_calls)
ig_media = [c for c in meta_calls if c[1] == "IG/media"]
ig_reels = [c[2] for c in ig_media if c[2].get("media_type") == "REELS"]
assert len(ig_reels) == 3
assert all(r["upload_type"] == "resumable" and r["share_to_feed"] == "true" for r in ig_reels)
assert [u[0] for u in uploads] == ["video-upload", "ig-api-upload"] * 3
caption = ig_reels[0]["caption"]
assert "Send this to a trader friend · Follow @dr.romeetsaha" in caption and DISCLAIMER in caption
assert "Follow me on X" not in caption
assert caption.endswith("#stockmarketindia #technicalanalysis #swingtrading #nse"), caption
print("ok  text, quote and chart posts all became Reels; send-to-a-friend captions")

# Each post also went to Stories on both.
ig_stories = [c[2] for c in ig_media if c[2].get("media_type") == "STORIES"]
assert len(ig_stories) == 3 and all(c["image_url"] == "https://cdn.example/l.jpg"
                                    for c in ig_stories)
assert len([c for c in meta_calls if c[1] == "PAGE/photo_stories"]) == 3
print("ok  every post shared to Instagram and Facebook Stories")

# 3. Next run: no new posts, retries only Instagram for 101.
before = len(meta_calls)
assert run() == 0
state = mirror.load_json(mirror.STATE_PATH, {})
assert state["pending"] == {}
assert "since_id" in x_calls[-1][1]
assert not any(c[1].startswith("PAGE/feed") for c in meta_calls[before:])
print("ok  retry posts only to Instagram, then pending is empty")

# 3b. A post with the paid group's Cosmofeed link stays on X only.
cosmo = {"data": [{"id": "104", "text": "Join the group https://t.co/c",
                   "created_at": "2026-10-25T06:00:00.000Z",
                   "attachments": {"media_keys": ["3_1"]},
                   "entities": {"urls": [{"url": "https://t.co/c",
                                          "expanded_url": "https://cosmofeed.com/vig/683f9c20"}]}}],
         "includes": TIMELINE["includes"], "meta": {"newest_id": "104"}}
timeline_queue.append(cosmo)
before = len(meta_calls)
assert run() == 0
state = mirror.load_json(mirror.STATE_PATH, {})
assert "104" not in state["pending"] and state["since_id"] == "104"
assert not any("104" in str(c) or "Join the group" in str(c) for c in meta_calls[before:])
quoting = {"id": "1", "text": "see", "media": [], "quote": {"text": "cosmofeed.com/vig/1"}}
assert mirror.is_skipped(quoting, mirror.load_json(mirror.CONFIG_PATH, {}))
print("ok  Cosmofeed-link post skipped on Facebook and Instagram")

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

# 6. A text post with a logo_link_match join link goes out as text + logo, no card.
cfg = mirror.load_json(mirror.CONFIG_PATH, {})
cfg["logo_link_match"] = ["t.me/"]
link_post = {"id": "200", "created_at": None, "media": [], "quote": None,
             "text": "Join telegram here: https://t.me/chartroom"}
assert mirror.uses_logo(link_post, cfg)
assert not mirror.uses_logo(dict(link_post, text="no link here"), cfg)
assert not mirror.uses_logo(dict(link_post, media=[{"type": "photo", "url": "u"}]), cfg)
mirror.make_card_file = lambda *a, **k: (_ for _ in ()).throw(AssertionError("card used"))
before = len(meta_calls)
mirror.publish({"post": link_post, "done": {}, "attempts": 0},
               {"user": {"username": "testuser"}}, cfg)
calls = meta_calls[before:]
fb = calls[0]
assert fb[1] == "PAGE/photos" and fb[2]["message"] == link_post["text"] and fb[3]
ig_caption = [c for c in calls if c[1] == "IG/media"][0][2]["caption"]
assert "https://t.me/chartroom" in ig_caption
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
assert state["pending"]["400"]["done"].get("facebook")
assert not state["pending"]["400"]["done"].get("instagram")
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
assert entry["done"] == {"facebook": True, "instagram": True,
                         "instagram_story": True, "facebook_story": True}
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
blank = os.path.join(tmp, "blank.jpg")
Image.new("RGB", (1080, 1350), "white").save(blank)
mirror.make_card_file = lambda post, state, cfg, text=None, page=None, size=None: \
    rendered.append(page) or blank
mirror.card_font_size = lambda text, name, handle: 40
before = len(meta_calls)
mirror.publish({"post": long_post, "attempts": 0, "done": {
    "facebook": True, "instagram_story": True, "facebook_story": True}},
    {"user": {"username": "testuser", "name": "Test User"}}, cfg)
calls = meta_calls[before:]
carousel = [c for c in calls if c[2].get("media_type") == "CAROUSEL"]
assert len(carousel) == 1 and len(carousel[0][2]["children"].split(",")) == len(pages)
assert rendered == ["{}/{}".format(i, len(pages)) for i in range(1, len(pages) + 1)]
items = [c[2] for c in calls if c[2].get("is_carousel_item")]
assert items[0]["alt_text"].startswith("Q2 FY27") and all(len(i["alt_text"]) <= 100 for i in items)
print("ok  long text post: carousel of {} cards on Instagram, with alt text".format(len(pages)))

# 14. Which posts become Reels where.
assert mirror.reel_kind(long_post, cfg, "instagram") is None
assert mirror.reel_kind(long_post, cfg, "facebook") == "cards"
assert mirror.reel_kind(link_post, dict(cfg, logo_link_match=["t.me/"]), "facebook") is None
two = dict(chart_post, id="700", media=[{"type": "photo", "url": "u1"},
                                        {"type": "photo", "url": "u2"}])
assert mirror.reel_kind(two, cfg, "instagram") == "charts"
assert mirror.reel_kind(dict(two, media=[{"type": "video", "url": "v"}]), cfg, "facebook") is None
print("ok  Reel choice: charts and short text everywhere, long text only on Facebook")

# 15. A two-chart post is one Reel showing each chart in turn (5 s each).
import imageio_ffmpeg
reel = mirror.make_reel(two, state, cfg, "charts")
frames, secs = imageio_ffmpeg.count_frames_and_secs(reel)
assert abs(secs - 10) < 0.5, secs
print("ok  two-chart Reel: {:.1f}s".format(secs))

# 16. Long text on Facebook: cards one after another, under Facebook's 90 s.
mirror.make_card_file = lambda post, state, cfg, text=None, page=None, size=None: blank
reel = mirror.make_reel(long_post, state, cfg, "cards")
frames, secs = imageio_ffmpeg.count_frames_and_secs(reel)
assert 10 < secs <= mirror.FB_REEL_MAX, secs
print("ok  long text Reel for Facebook: {:.1f}s".format(secs))

# 17. Weekly recap: the best charts by X engagement, as one carousel.
def chart(i, likes):
    return {"id": str(800 + i), "text": "Chart number {}\nmore".format(i),
            "created_at": "2026-10-2{}T05:00:00.000Z".format(i),
            "attachments": {"media_keys": ["k{}".format(i)]},
            "public_metrics": {"like_count": likes}}
paid = dict(chart(5, 999), text="Join https://cosmofeed.com/vig/1")
week = {"data": [chart(i, likes) for i, likes in enumerate([5, 50, 1, 30, 20])] + [paid]
        + [{"id": "799", "text": "just words", "public_metrics": {"like_count": 999}}],
        "includes": {"media": [{"media_key": "k{}".format(i), "type": "photo",
                                "url": "https://pbs.twimg.com/c{}.jpg".format(i)}
                               for i in range(6)]}}
cfg["recap"] = dict(cfg["recap"], charts=4)
timeline_queue[:] = [week]
before = len(meta_calls)
state = {"user": {"id": "42", "name": "Test User", "username": "testuser"}}
assert mirror.weekly_recap(state, cfg, "2026-W44") == 0
calls = meta_calls[before:]
assert "public_metrics" in x_calls[-1][1]["tweet.fields"]
ig_post = [c[2] for c in calls if c[2].get("media_type") == "CAROUSEL"][0]
assert len(ig_post["children"].split(",")) == 5, "cover + 4 charts"
assert "1. Chart number 1\n2. Chart number 3\n3. Chart number 4\n4. Chart number 0" \
    not in ig_post["caption"]
assert "1. Chart number 0 (20 Oct)\n2. Chart number 1 (21 Oct)\n3. Chart number 3 (23 Oct)\n" \
    "4. Chart number 4 (24 Oct)" in ig_post["caption"], "best 4 by likes, in date order"
fb_post = [c[2] for c in calls if c[1] == "PAGE/feed"][0]
assert fb_post["message"].startswith("📊 This week's top charts") and "attached_media[4]" in fb_post
assert state["recap"]["finished"] and state["recap"]["done"] == {"facebook": True, "instagram": True}
before = len(meta_calls)
assert mirror.weekly_recap(state, cfg, "2026-W44") == 0 and len(meta_calls) == before
assert "cosmofeed" not in ig_post["caption"], "paid-link chart left out"
print("ok  weekly recap: cover + best 4 charts on both, posted once")

# 18. The recap is due on Sunday from 11:00 India time, not before.
from datetime import datetime, timezone
mirror.now_utc = lambda: datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)  # Sun 10:30 IST
assert mirror.recap_week(cfg) is None
mirror.now_utc = lambda: datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)  # Sun 11:30 IST
assert mirror.recap_week(cfg) == "2026-W44"
mirror.now_utc = lambda: datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)  # Mon
assert mirror.recap_week(cfg) is None
timeline_queue[:] = [{"data": [chart(0, 1)], "includes": week["includes"]}]
state = {"user": {"id": "42", "name": "Test User", "username": "testuser"}}
before = len(meta_calls)
assert mirror.weekly_recap(state, cfg, "2026-W45") == 0 and len(meta_calls) == before
assert state["recap"]["finished"]
print("ok  recap due Sunday 11:00 IST; skipped in a week with too few charts")
print("ALL TESTS PASSED")
