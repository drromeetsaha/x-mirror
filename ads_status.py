"""Read-only: print the review details of one ad (status, issues, review
feedback). Run by the "Ads ad status" workflow."""

import json
import os
import urllib.parse
import urllib.request

AD = os.environ.get("AD_ID", "52598526935908")
GRAPH = "https://graph.facebook.com/v26.0/"


def get(path, **params):
    params["access_token"] = os.environ["META_ADS_TOKEN"]
    try:
        with urllib.request.urlopen(GRAPH + path + "?" + urllib.parse.urlencode(params), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


for fields in ("name,status,configured_status,effective_status,ad_review_feedback,issues_info",
               "name,effective_status,recommendations", "name,adset{effective_status,issues_info,name}"):
    print(fields.split(",")[0], "->", json.dumps(get(AD, fields=fields), indent=1)[:2500])
