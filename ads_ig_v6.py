"""Create the Instagram profile-visits ad with the v6 video and switch it on
(at the owner's request). Broad targeting: India, 21-55, no interests,
Instagram feed and Reels. Nothing existing is changed. Run by the "Ads
Instagram v6" workflow."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
CAMPAIGN = "52597276523508"        # ChartRoom - Instagram profile visits (test)
OLD_ADSET = "52597782791308"       # v2 ad set (rejected), used as the template
OLD_CREATIVE = "954464764099543"
VIDEO_ID = "1520452473238271"      # v6 video, already uploaded for the Facebook ad
THUMB = "ads/chartroom-ad-v6.jpg"
NAME = "India traders - profile visits v6 (education shapes)"
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


old = call("GET", OLD_ADSET, fields="targeting,bid_strategy,billing_event,optimization_goal,"
           "daily_budget,promoted_object,destination_type")
spec = call("GET", OLD_CREATIVE, fields="object_story_spec")["object_story_spec"]
vd = spec["video_data"]
print("Old ad wording: " + json.dumps({k: vd.get(k) for k in ("message", "title", "call_to_action")}))

t = old["targeting"]
for k in ("flexible_spec", "interests", "behaviors", "exclusions", "targeting_automation"):
    t.pop(k, None)
t["age_min"], t["age_max"] = 21, 55
t["geo_locations"] = {"countries": ["IN"], "location_types": ["home", "recent"]}
t["targeting_automation"] = {"advantage_audience": 0}
t["publisher_platforms"] = ["instagram"]
t["instagram_positions"] = ["stream", "reels"]
for k in ("facebook_positions", "device_platforms", "messenger_positions", "audience_network_positions"):
    t.pop(k, None)
start = datetime.now(timezone.utc)
adset = dict(name=NAME, campaign_id=CAMPAIGN, optimization_goal=old["optimization_goal"],
             destination_type=old["destination_type"], billing_event=old["billing_event"],
             bid_strategy=old.get("bid_strategy", "LOWEST_COST_WITHOUT_CAP"),
             daily_budget=old["daily_budget"], targeting=t,
             start_time=start.strftime("%Y-%m-%dT%H:%M:%S+0000"),
             end_time=(start + timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
             status="ACTIVE")
if old.get("promoted_object"):
    adset["promoted_object"] = old["promoted_object"]
print("New ad set: " + json.dumps({k: v for k, v in adset.items() if k != "targeting"}))
print("Targeting: " + json.dumps(t))

call("POST", AD_ACCOUNT + "/adsets", execution_options=["validate_only"], **adset)
print("check: Meta accepts the new ad set.")

with open(THUMB, "rb") as f:
    images = call("POST", AD_ACCOUNT + "/adimages", files={"filename": f})["images"]
vd["video_id"] = VIDEO_ID
vd["image_hash"] = next(iter(images.values()))["hash"]
vd.pop("image_url", None)
vd["message"] = MESSAGE
creative = call("POST", AD_ACCOUNT + "/adcreatives", name="ChartRoom IG ad v6 shapes",
                object_story_spec=spec)["id"]
adset_id = call("POST", AD_ACCOUNT + "/adsets", **adset)["id"]
ad_id = call("POST", AD_ACCOUNT + "/ads", name="ChartRoom IG ad v6", adset_id=adset_id,
             creative={"creative_id": creative}, status="ACTIVE")["id"]
print("Created and switched on: creative {} · ad set {} · ad {}. Nothing else changed.".format(
    creative, adset_id, ad_id))
print("Ad status: " + json.dumps(call("GET", ad_id, fields="status,effective_status")))
