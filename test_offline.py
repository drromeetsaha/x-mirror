"""Offline check of mirror.py: fake X responses, record Meta calls.

Run: python test_offline.py   (needs Chrome for the card render)
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
    return {"id": "m{}".format(len(meta_calls)), "status_code": "FINISHED",
            "images": [{"width": 720, "source": "https://cdn.example/s.jpg"},
                       {"width": 1440, "source": "https://cdn.example/l.jpg"}]}


mirror.x_get = fake_x_get
mirror.graph = fake_graph
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
assert run() == 1
state = mirror.load_json(mirror.STATE_PATH, {})
timeline_params = x_calls[-1][1]
assert timeline_params["exclude"] == "replies,retweets"
assert "start_time" in timeline_params and "since_id" not in timeline_params
assert state["since_id"] == "103"
assert list(state["pending"]) == ["101"]
assert state["pending"]["101"]["done"] == {"facebook": True}
print("ok  failed Instagram post kept for retry, Facebook not repeated")

fb_feed = [c for c in meta_calls if c[1] == "PAGE/feed"]
assert fb_feed[0][2]["message"] == "Plain thought. Read https://example.com/article"
fb_photos = [c for c in meta_calls if c[1] == "PAGE/photos" and c[2].get("message")]
assert fb_photos[0][2]["message"] == "Agree with this & more" and fb_photos[0][3]
assert fb_photos[1][2]["url"] == "https://pbs.twimg.com/media/abc.jpg?name=large"
assert fb_photos[1][2]["message"] == "Look at this chart"
ig_media = [c for c in meta_calls if c[1] == "IG/media"]
assert all(c[2]["image_url"] == "https://cdn.example/l.jpg" for c in ig_media)
assert ig_media[0][2]["caption"].endswith("Follow me on X @testuser")
print("ok  links expanded, media links dropped, quote shown as card, IG captions")

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
print("ALL TESTS PASSED")
