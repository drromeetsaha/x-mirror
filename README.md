# x-mirror

Copies new posts from X (@DrRomeetSaha) to the Facebook Page *The ChartRoom*
and Instagram *@dr.romeetsaha*. It runs on GitHub Actions every 10 minutes.

**What starts each run:** a cron-job.org job sends a `workflow_dispatch` every
10 minutes, using a fine-grained token limited to Actions on this repo. That
token is not one of the secrets below. GitHub's own `schedule` stays in the
workflow as a backup, but it proved unreliable.

- **Copies:** original posts and quote posts. Replies and reposts are skipped.
- **Stays on X:** any post (or quoted post) with a link from `skip_link_match`
  (the paid group's Cosmofeed link) is never copied, and never appears in a
  Story or the weekly recap.
- **Reels:** most posts go out as a **Reel** on Instagram and Facebook,
  because Reels are what both apps show to people who don't follow yet
  (`reel.py`). A chart sits in The ChartRoom frame, with the post's first
  line as a title, and slowly zooms towards the latest candles (10 s; with
  several charts, 5 s each, one after another). A text or quote post shows
  its card. If a Reel fails before anything is published, the post goes out
  the old way (photo, carousel or text) instead.
- **Long text posts:** a carousel of numbered cards on Instagram (easier to
  read than a long Reel); a Reel of the cards on Facebook (90 s at most).
- **Videos:** copied as they are (a Reel on Instagram).
- **Stories:** every post except videos and join-link posts is also shared
  to Instagram and Facebook Stories as a 9:16 picture. A Story that fails is
  skipped, not retried; Instagram Stories pause when fewer than 10 of the
  day's 50 API posts are left.
- **Weekly recap:** Sundays from 11:00 India time, one carousel on both: a
  cover listing the week's best charts (ranked by likes, reposts, quotes,
  bookmarks and replies on X), then each chart framed. Skipped in a week
  with fewer than 3 chart posts.
- **Captions:** post text, then "Send this to a trader friend · Follow …"
  (sends are what Instagram weighs most for reaching new people), a short
  disclaimer and up to 5 hashtags in all (Instagram's limit), matched from
  the post's words. Facebook gets the text, its own share line and the
  disclaimer. Instagram images carry short alt text.
- **Settings** in `config.json`: `ig_footer`, `fb_footer`, `frame_ask`,
  `disclaimer`, `skip_link_match` (links that keep a post on X only), `hashtags` (keyword → tag, plus general ones), `chart_reels`
  (which platforms get Reels; `[]` turns them off), `text_reels` (Reels for
  text posts too), `reel_seconds`, `reel_seconds_each`, `stories` (`[]` turns
  them off), `recap` (`enabled`, `weekday` 0=Mon…6=Sun, `hour`, `charts`,
  `min_charts`), `frame_charts`, `ig_trial_reels` (set to `"SS_PERFORMANCE"`
  to try Instagram's trial Reels, shown to non-followers first; not yet
  tested), `card_footer`.
- **Progress:** kept in `state.json`, which the workflow commits back, so
  nothing is posted twice.

## Secrets (repo → Settings → Secrets and variables → Actions)

| Name | What |
|---|---|
| `X_API_KEY`, `X_API_SECRET` | X app Consumer Key and Secret |
| `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | X Access Token and Secret for @DrRomeetSaha |
| `FB_PAGE_ID` | The ChartRoom Page ID |
| `IG_USER_ID` | Instagram business account ID |
| `FB_PAGE_TOKEN` | Page access token (never expires) |

## Running it by hand

Go to **Actions → Mirror X posts → Run workflow**. Two options:

- **Dry run:** reads from X and shows what would be posted, without posting
  anything.
- **Test post ID:** mirrors one X post right away. The ID is the number at the
  end of the post's URL.

A dry run can also be started on another branch (`gh workflow run mirror.yml
--ref <branch> -f dry_run=true`) to try changes before merging them into
`main`. Its `card-previews` artifact holds the cards, frames, Stories and
Reels. Tick **recap** as well to preview this week's recap instead.

## Ads report

**Actions → Ads report → Run workflow** prints the Meta ads' spend, reach,
results and cost per result, plus follower counts (`ads_report.py`, read-only,
uses the `META_ADS_TOKEN` secret of the `chartroom-ads` system user), with a
breakdown by placement. **Actions → Ads change** swapped the Instagram ad for
v2 on 2026-11-04 (`ads_change.py`; "check" validates, "apply" changes).

## Renewing the Meta token (about every 90 days)

When access is 10 days from ending, one run a day fails and GitHub emails you.
To renew:

1. Open the Graph API Explorer. Choose app **x-mirror**, then **Get User Access
   Token**, keeping the same 5 permissions. Click **Generate Access Token** and
   tick The ChartRoom and @dr.romeetsaha.
2. Paste that token into the Access Token Debugger, click **Debug**, then
   **Extend Access Token**.
3. Run `GET /1199609839893751?fields=access_token` with the extended token.
4. Put the returned Page token into the `FB_PAGE_TOKEN` secret.

## Costs

- **X:** about $0.001 per post read (~$0.20 a month at 6 posts a day).
  Credits are prepaid at console.x.com.
- **Meta and GitHub Actions** (public repo): free.
