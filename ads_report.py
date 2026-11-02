"""Print how the Meta ads are doing: spend, reach and results per ad set,
plus current follower counts. Read-only. Run by the "Ads report" workflow."""

import json
import os
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v26.0/"
AD_ACCOUNT = "act_1064546756636439"
PAGE_ID = "1199609839893751"
IG_USER_ID = "17841475622662431"
RESULTS = {"PROFILE_VISIT": "ig_profile_visit", "PAGE_LIKES": "like"}


def get(path, **params):
    params["access_token"] = os.environ["META_ADS_TOKEN"]
    url = GRAPH + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main():
    acct = get(AD_ACCOUNT, fields="amount_spent,spend_cap,funding_source_details")
    print("Account: spent ₹{:.2f} of ₹{:.2f} cap; {}".format(
        int(acct.get("amount_spent", 0)) / 100, int(acct.get("spend_cap", 0)) / 100,
        acct.get("funding_source_details", {}).get("display_string", "")))
    sets = get(AD_ACCOUNT + "/adsets", fields="name,effective_status,optimization_goal,"
               "daily_budget,start_time,end_time")
    for s in sets.get("data", []):
        print("\n== {} [{}] ₹{}/day, {} → {}".format(
            s["name"], s["effective_status"], int(s["daily_budget"]) // 100,
            s["start_time"][:10], s["end_time"][:10]))
        ins = get(s["id"] + "/insights", date_preset="maximum",
                  fields="spend,impressions,reach,frequency,clicks,actions").get("data", [])
        if not ins:
            print("  no delivery yet")
        else:
            i = ins[0]
            actions = {a["action_type"]: int(float(a["value"])) for a in i.get("actions", [])}
            print("  spend ₹{} · reach {} · impressions {} · frequency {:.2f} · clicks {}".format(
                i["spend"], i["reach"], i["impressions"], float(i.get("frequency", 0)),
                i.get("clicks", 0)))
            key = RESULTS.get(s["optimization_goal"])
            hits = actions.get(key) or 0
            cost = "₹{:.2f} each".format(float(i["spend"]) / hits) if hits else "n/a"
            print("  results ({}): {} ({})".format(key, hits, cost))
            print("  all actions: " + ", ".join("{}={}".format(k, v)
                                                for k, v in sorted(actions.items())))
        ads = get(s["id"] + "/ads", fields="name,effective_status,issues_info")
        for a in ads.get("data", []):
            print("  ad {}: {}".format(a["name"], a["effective_status"]))
            for issue in a.get("issues_info", []):
                print("    issue: " + issue.get("error_message", str(issue)))
    ig = get(IG_USER_ID, fields="followers_count,media_count")
    page = get(PAGE_ID, fields="followers_count,fan_count")
    print("\nFollowers now: Instagram {} · Facebook Page {} followers ({} likes)".format(
        ig.get("followers_count", "?"), page.get("followers_count", "?"),
        page.get("fan_count", "?")))
    for name, j in (("Instagram", ig), ("Page", page)):
        if "error" in j:
            print("  ({} count unavailable: {})".format(name, j["error"].get("message")))


if __name__ == "__main__":
    main()
