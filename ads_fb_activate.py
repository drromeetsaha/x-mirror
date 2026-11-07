"""Switch on the Facebook v6 ad set (the only change). Run by hand from the
"Ads Facebook switch on" workflow, at the owner's request."""

import json
import os
import sys

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
ADSET = "52598526803908"
CAMPAIGN = "52597277709908"
AD = "52598526935908"


def call(method, path, **data):
    data["access_token"] = os.environ["META_ADS_TOKEN"]
    r = (requests.get(GRAPH + path, params=data, timeout=60) if method == "GET"
         else requests.post(GRAPH + path, data=data, timeout=60))
    j = r.json()
    if "error" in j:
        sys.exit("{} {} failed: {}".format(method, path, j["error"].get("message")))
    return j


print("Campaign:", call("GET", CAMPAIGN, fields="name,effective_status"))
call("POST", ADSET, status="ACTIVE")
print("Ad set:", call("GET", ADSET, fields="name,status,effective_status,daily_budget,start_time,end_time"))
print("Ad:", json.dumps(call("GET", AD, fields="name,status,effective_status,ad_review_feedback")))
