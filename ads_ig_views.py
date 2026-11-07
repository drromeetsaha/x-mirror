"""Last Instagram attempt: a video-views ad (Engagement objective, THRUPLAY, no
Instagram-profile destination) using the approved v6 video and wording, broad
targeting, Instagram feed and Reels. Created and switched on at the owner's
request. Nothing existing is changed. Run by the "Ads Instagram video views"
workflow.

Several accepted setups are tried with validate_only first; nothing is created
unless one of them passes."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
PAGE_ID = "1199609839893751"
IG_USER_ID = "17841475622662431"
FB_CREATIVE = "1651110986562990"   # approved v6 Facebook creative (video + wording)
CAMPAIGN_NAME = "ChartRoom - Instagram video views (last try)"
ADSET_NAME = "India traders - IG video views v6 (education shapes)"
DAYS = 7


def call(method, path, **data):
    data["access_token"] = os.environ["META_ADS_TOKEN"]
    if method == "GET":
        r = requests.get(GRAPH + path, params=data, timeout=120)
    else:
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in data.items()}
        r = requests.post(GRAPH + path, data=data, timeout=600)
    return r.json()


def must(j, what):
    if "error" in j:
        e = j["error"]
        sys.exit("{} failed: {} {}".format(what, e.get("message"), e.get("error_user_msg", "")))
    return j


spec = must(call("GET", FB_CREATIVE, fields="object_story_spec"), "read creative")["object_story_spec"]
spec["video_data"].pop("call_to_action", None)
spec["video_data"].pop("image_url", None)   # keep image_hash only
print("Creative text: " + json.dumps(spec["video_data"].get("message")))

start = datetime.now(timezone.utc)
base_targeting = {"age_min": 21, "age_max": 55,
                  "geo_locations": {"countries": ["IN"], "location_types": ["home", "recent"]},
                  "targeting_automation": {"advantage_audience": 0},
                  "publisher_platforms": ["instagram"]}


def adset_args(campaign_id, positions, promoted, status):
    t = dict(base_targeting)
    if positions:
        t["instagram_positions"] = ["stream", "reels"]
    a = dict(name=ADSET_NAME, campaign_id=campaign_id, optimization_goal="THRUPLAY",
             destination_type="ON_VIDEO", billing_event="IMPRESSIONS",
             bid_strategy="LOWEST_COST_WITHOUT_CAP", daily_budget="9800", targeting=t,
             start_time=start.strftime("%Y-%m-%dT%H:%M:%S+0000"),
             end_time=(start + timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
             status=status)
    if promoted:
        a["promoted_object"] = {"page_id": PAGE_ID}
    return a


camp = "52598536521908"   # empty campaign created by the first run of this script
print("Using campaign: " + camp)

chosen = None
for positions in (True, False):
    for promoted in (False, True):
        a = adset_args(camp, positions, promoted, "ACTIVE")
        j = call("POST", AD_ACCOUNT + "/adsets", execution_options=["validate_only"], **a)
        if "error" in j:
            print("try positions={} promoted_object={}: {} {}".format(
                positions, promoted, j["error"].get("message"), j["error"].get("error_user_msg", "")))
            continue
        print("check: ad set accepted with positions={} promoted_object={}".format(positions, promoted))
        chosen = a
        break
    if chosen:
        break
if not chosen:
    call("POST", camp, status="PAUSED")
    sys.exit("No ad set setup was accepted by Meta. The empty campaign was paused. Nothing else created.")

creative = must(call("POST", AD_ACCOUNT + "/adcreatives", name="ChartRoom IG video views v6",
                     object_story_spec=spec, instagram_user_id=IG_USER_ID), "create creative")["id"]
adset_id = must(call("POST", AD_ACCOUNT + "/adsets", **chosen), "create ad set")["id"]
ad_id = must(call("POST", AD_ACCOUNT + "/ads", name="ChartRoom IG video views v6", adset_id=adset_id,
                  creative={"creative_id": creative}, status="ACTIVE"), "create ad")["id"]
print("Created and switched on: campaign {} · creative {} · ad set {} · ad {}. Nothing else changed.".format(
    camp, creative, adset_id, ad_id))
print("Ad status: " + json.dumps(call("GET", ad_id, fields="status,effective_status")))
