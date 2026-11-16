"""Post one ChartRoom learning video to the Facebook Page and Instagram.

Started by .github/workflows/learnings.yml. Bella (the Grok Bot routine) sends
the daily 7 PM lesson to Telegram, then opens a GitHub issue titled
"learning-video ..." whose body is:

    file_id: <Telegram file_id of the mp4>
    caption: <caption text, may span several lines>

This script downloads that video from Telegram, then posts it to Facebook and
Instagram with the same footer, disclaimer and hashtags x-mirror uses. It never
touches X.

Environment:
  LEARNINGS_BOT_TOKEN        Telegram bot that sent the video (can fetch its file_id)
  FB_PAGE_ID, FB_PAGE_TOKEN, IG_USER_ID   same Meta secrets as mirror.py
  ISSUE_BODY                 body of the issue (file_id + caption), or
  FILE_ID, CAPTION           the same, given directly (manual run)
  PLATFORMS                  both (default), facebook or instagram
  DRY_RUN=1                  download and show what would be posted; post nothing
"""

import os
import re
import sys

import requests

import mirror as m

RESULT_PATH = os.environ.get("RESULT_PATH", "learnings_result.txt")


def parse_inputs():
    body = os.environ.get("ISSUE_BODY", "")
    file_id = os.environ.get("FILE_ID", "").strip()
    caption = os.environ.get("CAPTION", "").strip()
    if body:
        found = re.search(r"file_id:\s*(\S+)", body)
        if found:
            file_id = found.group(1)
        cap = re.search(r"caption:\s*(.*)", body, re.S | re.I)
        if cap:
            caption = cap.group(1).strip()
    if not file_id:
        raise SystemExit("No file_id given.")
    if not caption:
        raise SystemExit("No caption given.")
    return file_id, caption


def download_video(file_id):
    """Fetch the mp4 from Telegram. Error text never includes the URL, which
    contains the bot token."""
    token = os.environ["LEARNINGS_BOT_TOKEN"]
    try:
        info = requests.get("https://api.telegram.org/bot{}/getFile".format(token),
                            params={"file_id": file_id}, timeout=60).json()
        if not info.get("ok"):
            raise RuntimeError(str(info.get("description")))
        resp = requests.get("https://api.telegram.org/file/bot{}/{}".format(
            token, info["result"]["file_path"]), timeout=300)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 - keep the token out of any message
        raise RuntimeError("Telegram download failed: {}".format(
            type(e).__name__ if "api.telegram.org" in str(e) else e))
    path = os.path.join(m.WORK, "lesson.mp4")
    with open(path, "wb") as f:
        f.write(resp.content)
    print("downloaded {:.1f} MB".format(len(resp.content) / 1e6))
    m.keep_preview(path)
    return path


def post_facebook(video, caption, cfg):
    msg = "\n\n".join(p for p in (caption, cfg.get("fb_footer"),
                                  cfg.get("disclaimer")) if p)
    # A normal Page video, not a Reel: lessons run about 2 minutes and Facebook
    # Reels stop at 90 seconds. Short videos are shown as Reels there anyway.
    with open(video, "rb") as f:
        m.graph("POST", "{}/videos".format(m.page()),
                data={"description": msg}, files={"source": f})


def post_instagram(video, caption, cfg):
    text = m.ig_caption({"text": caption}, {"user": {"username": ""}}, cfg)
    container = m.graph("POST", "{}/media".format(m.ig()), data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": text, "share_to_feed": "true"})
    m.rupload("ig-api-upload", container["id"], video)
    m.wait_until_ready(container["id"])
    m.publish_container(container["id"])


def main():
    file_id, caption = parse_inputs()
    cfg = m.load_json(m.CONFIG_PATH, {})
    platforms = os.environ.get("PLATFORMS", "both").strip().lower() or "both"
    video = download_video(file_id)
    results = []
    for name, fn in (("facebook", post_facebook), ("instagram", post_instagram)):
        if platforms not in ("both", name):
            continue
        try:
            fn(video, caption, cfg)
            results.append("{}: posted{}".format(name, " (dry run)" if m.DRY_RUN else ""))
        except Exception as e:  # noqa: BLE001 - report each platform separately
            results.append("{}: FAILED - {}".format(name, str(e)[:300]))
    print("\n".join(results))
    with open(RESULT_PATH, "w") as f:
        f.write("\n".join(results))
    if any("FAILED" in r for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
