"""Pause every ACTIVE campaign, ad set and ad in the ad account. Run by the
"Ads pause all" workflow, by hand only. Nothing is deleted."""
import json, os, sys
import requests

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
TOKEN = os.environ["META_ADS_TOKEN"]


def get(path, **p):
    p["access_token"] = TOKEN
    return requests.get(GRAPH + path, params=p, timeout=120).json()


def pause(obj_id):
    r = requests.post(GRAPH + obj_id, data={"status": "PAUSED", "access_token": TOKEN}, timeout=120).json()
    return r


for kind in ("campaigns", "adsets", "ads"):
    d = get(AD_ACCOUNT + "/" + kind, fields="name,status,effective_status", limit=200)
    if "error" in d:
        sys.exit(json.dumps(d["error"]))
    for o in d.get("data", []):
        if o.get("status") == "ACTIVE":
            print(kind, o["id"], o["name"], "->", pause(o["id"]))
print("--- after")
for kind in ("campaigns", "adsets", "ads"):
    for o in get(AD_ACCOUNT + "/" + kind, fields="name,status,effective_status", limit=200).get("data", []):
        print(kind, o["name"], o["status"], o["effective_status"])
