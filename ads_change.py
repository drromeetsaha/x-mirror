"""Swap the Instagram profile-visits ad for v2: the ad video without its
3-second still intro, Instagram feed and Reels only (Stories brought mostly
accidental taps), and Advantage+ audience, with the old interests as
suggestions. Run by the "Ads change" workflow.

MODE=check validates everything without changing the ads; MODE=apply uploads
the video, starts the new ad set and pauses the old one."""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
CAMPAIGN = "52597276523508"
OLD_ADSET = "52597276553508"
OLD_CREATIVE = "1136217066007078"
VIDEO = "ads/chartroom-ad-v2.mp4"
THUMB = "ads/chartroom-ad-v2.jpg"
NAME = "India traders - profile visits v2 (Adv+ audience, feed+reels)"


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
        sys.exit("{} {} failed: {} {}".format(method, path, e.get("message"),
                                              e.get("error_user_msg", "")))
    return j


def main():
    apply = os.environ.get("MODE") == "apply"
    old = call("GET", OLD_ADSET, fields="targeting,end_time,bid_strategy,billing_event,"
               "daily_budget,promoted_object,destination_type,effective_status")
    spec = call("GET", OLD_CREATIVE, fields="object_story_spec")["object_story_spec"]

    targeting = old["targeting"]
    targeting["publisher_platforms"] = ["instagram"]
    targeting["instagram_positions"] = ["stream", "reels"]
    targeting["targeting_automation"] = {"advantage_audience": 1}
    # With Advantage+ audience the age range is only a suggestion and must reach 65.
    targeting["age_max"] = 65
    for k in ("facebook_positions", "device_platforms", "messenger_positions",
              "audience_network_positions"):
        targeting.pop(k, None)
    adset = dict(name=NAME, campaign_id=CAMPAIGN, optimization_goal="PROFILE_VISIT",
                 destination_type=old["destination_type"], billing_event=old["billing_event"],
                 bid_strategy=old.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP"),
                 daily_budget=old["daily_budget"], targeting=targeting,
                 start_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                 end_time=old["end_time"], status="PAUSED")
    if old.get("promoted_object"):
        adset["promoted_object"] = old["promoted_object"]
    print("Old ad set: {} ({}), ends {}".format(OLD_ADSET, old["effective_status"],
                                                old["end_time"]))
    print("New ad set: " + json.dumps({k: v for k, v in adset.items() if k != "targeting"}))
    print("Targeting: " + json.dumps(targeting))
    print("Video: {} ({} KB)".format(VIDEO, os.path.getsize(VIDEO) // 1024))

    if not apply:
        call("POST", AD_ACCOUNT + "/adsets", execution_options=["validate_only"], **adset)
        print("check: Meta accepts the new ad set. Nothing was changed.")
        return

    with open(VIDEO, "rb") as f:
        video_id = call("POST", AD_ACCOUNT + "/advideos", files={"source": f},
                        name="ChartRoom ad v2")["id"]
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

    vd = spec["video_data"]
    vd["video_id"] = video_id
    vd["image_hash"] = image_hash
    vd.pop("image_url", None)
    creative = call("POST", AD_ACCOUNT + "/adcreatives", name="ChartRoom ad v2",
                    object_story_spec=spec)["id"]
    adset_id = call("POST", AD_ACCOUNT + "/adsets", **adset)["id"]
    ad_id = call("POST", AD_ACCOUNT + "/ads", name="ChartRoom ad v2", adset_id=adset_id,
                 creative={"creative_id": creative}, status="ACTIVE")["id"]
    call("POST", adset_id, status="ACTIVE")
    call("POST", OLD_ADSET, status="PAUSED")
    print("Live: video {} · creative {} · ad set {} · ad {}. Old ad set {} paused.".format(
        video_id, creative, adset_id, ad_id, OLD_ADSET))


if __name__ == "__main__":
    main()
