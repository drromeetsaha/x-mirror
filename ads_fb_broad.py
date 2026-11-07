"""Make the new Facebook v6 ad set broad: India, age 21-55, no interests.
Only the targeting of that one paused ad set changes. Run by the "Ads Facebook
broad targeting" workflow."""

import json
import os
import sys

import requests

GRAPH = "https://graph.facebook.com/v26.0/"
ADSET = "52598526803908"   # India traders - Page likes v6 (education shapes)


def call(method, path, **data):
    data["access_token"] = os.environ["META_ADS_TOKEN"]
    if method == "GET":
        r = requests.get(GRAPH + path, params=data, timeout=120)
    else:
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in data.items()}
        r = requests.post(GRAPH + path, data=data, timeout=120)
    j = r.json()
    if "error" in j:
        e = j["error"]
        sys.exit("{} {} failed: {} {}".format(method, path, e.get("message"), e.get("error_user_msg", "")))
    return j


def main():
    cur = call("GET", ADSET, fields="name,effective_status,targeting")
    t = cur["targeting"]
    print("Before: " + json.dumps(t))
    for k in ("flexible_spec", "interests", "behaviors", "exclusions"):
        t.pop(k, None)
    t["age_min"], t["age_max"] = 21, 55
    t["geo_locations"] = {"countries": ["IN"], "location_types": ["home", "recent"]}
    t["targeting_automation"] = {"advantage_audience": 0}
    call("POST", ADSET, targeting=t)
    now = call("GET", ADSET, fields="name,effective_status,targeting")
    print("After: " + json.dumps(now["targeting"]))
    print("Ad set {} is {}. Nothing else changed.".format(now["name"], now["effective_status"]))


if __name__ == "__main__":
    main()
