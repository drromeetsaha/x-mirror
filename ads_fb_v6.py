"""Create a new Facebook Page-likes ad using the v6 video (drawn shapes, no
charts). Everything is created PAUSED; nothing is switched on and nothing old is
changed. Run by the "Ads new Facebook ad" workflow.

MODE=check prints the old ad's wording, the account limits and validates the
new ad set without creating anything. MODE=apply uploads the video and creates
the ad set and ad, both paused."""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
CAMPAIGN = "52597277709908"          # ChartRoom - Facebook Page likes (test)
OLD_ADSET = "52597277763508"
OLD_CREATIVE = "3044915435858256"
VIDEO = "ads/chartroom-ad-v6.mp4"
THUMB = "ads/chartroom-ad-v6.jpg"
NAME = "India traders - Page likes v6 (education shapes)"
MESSAGE = ("Free chart-reading lessons from The ChartRoom. Illustration only. "
           "Educational content, not investment advice.")
DAYS = 7


def call(method, path, files=None, **data):
    data["access_token"] = os.environ["META_ADS_TOKEN"]
    if method == "GET":
        r = requests.get(GRAPH + path, params=data, timeout=120)
    else:
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in data.items()}
        r = requests.post(GRAPH + path, data=data, files=files, timeout=600)
    j = r.json()
    if "error" in j:
        e = j["error"]
        sys.exit("{} {} failed: {} {}".format(method, path, e.get("message"), e.get("error_user_msg", "")))
    return j


def main():
    apply = os.environ.get("MODE") == "apply"
    old = call("GET", OLD_ADSET, fields="targeting,bid_strategy,billing_event,optimization_goal,"
               "daily_budget,promoted_object,destination_type,effective_status,end_time")
    spec = call("GET", OLD_CREATIVE, fields="object_story_spec")["object_story_spec"]
    acct = call("GET", AD_ACCOUNT, fields="spend_cap,amount_spent,balance,currency")

    vd = spec["video_data"]
    print("Old ad wording: " + json.dumps({k: vd.get(k) for k in ("message", "title", "link_description",
                                                                 "call_to_action")}))
    print("Account: spend cap {} · spent so far {} · balance {} ({}; amounts in minor units)".format(
        acct.get("spend_cap"), acct.get("amount_spent"), acct.get("balance"), acct.get("currency")))

    targeting = old["targeting"]
    pos = targeting.get("facebook_positions")
    if pos:
        targeting["facebook_positions"] = [p for p in pos if p != "story"]
    start = datetime.now(timezone.utc)
    adset = dict(name=NAME, campaign_id=CAMPAIGN, optimization_goal=old["optimization_goal"],
                 destination_type=old.get("destination_type", "ON_PAGE"),
                 billing_event=old["billing_event"],
                 bid_strategy=old.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP"),
                 daily_budget=old["daily_budget"], targeting=targeting,
                 start_time=start.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                 end_time=(start + timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                 status="PAUSED")
    if old.get("promoted_object"):
        adset["promoted_object"] = old["promoted_object"]
    print("Old ad set: {} ({}), ends {}".format(OLD_ADSET, old["effective_status"], old.get("end_time")))
    print("New ad set: " + json.dumps({k: v for k, v in adset.items() if k != "targeting"}))
    print("Targeting: " + json.dumps(targeting))
    print("Video: {} ({} KB)".format(VIDEO, os.path.getsize(VIDEO) // 1024))
    print("New text: " + MESSAGE)

    if not apply:
        call("POST", AD_ACCOUNT + "/adsets", execution_options=["validate_only"], **adset)
        print("check: Meta accepts the new ad set. Nothing was created or changed.")
        return

    with open(VIDEO, "rb") as f:
        video_id = call("POST", AD_ACCOUNT + "/advideos", files={"source": f},
                        name="ChartRoom ad v6 shapes")["id"]
    with open(THUMB, "rb") as f:
        images = call("POST", AD_ACCOUNT + "/adimages", files={"filename": f})["images"]
    image_hash = next(iter(images.values()))["hash"]
    for _ in range(60):
        status = call("GET", video_id, fields="status")["status"]["video_status"]
        if status == "ready":
            break
        time.sleep(10)
    else:
        sys.exit("video {} not ready after 10 minutes".format(video_id))

    vd["video_id"] = video_id
    vd["image_hash"] = image_hash
    vd.pop("image_url", None)
    vd["message"] = MESSAGE
    creative = call("POST", AD_ACCOUNT + "/adcreatives", name="ChartRoom ad v6 shapes",
                    object_story_spec=spec)["id"]
    adset_id = call("POST", AD_ACCOUNT + "/adsets", **adset)["id"]
    ad_id = call("POST", AD_ACCOUNT + "/ads", name="ChartRoom FB ad v6", adset_id=adset_id,
                 creative={"creative_id": creative}, status="PAUSED")["id"]
    print("Created, all PAUSED: video {} · creative {} · ad set {} · ad {}. Nothing else changed.".format(
        video_id, creative, adset_id, ad_id))


if __name__ == "__main__":
    main()
