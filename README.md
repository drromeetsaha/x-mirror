# x-mirror

Copies new posts from X (@DrRomeetSaha) to the Facebook Page *The ChartRoom*
and Instagram *@dr.romeetsaha*. It runs on GitHub Actions every 10 minutes.

**What starts each run:** a cron-job.org job sends a `workflow_dispatch` every
10 minutes, using a fine-grained token limited to Actions on this repo. That
token is not one of the secrets below. GitHub's own `schedule` stays in the
workflow as a backup, but it proved unreliable.

- **Copies:** original posts and quote posts. Replies and reposts are skipped.
- **Single-chart posts:** become a 10-second **Reel** on Instagram and
  Facebook. The chart sits in The ChartRoom frame, with the post's first line
  as a title, and slowly zooms towards the latest candles (`reel.py`). If a
  Reel fails before anything is published, the photo is posted instead.
- **Several photos:** an Instagram carousel, each slide framed. On Facebook
  they are posted as they are.
- **Text-only and quote posts:** an image card for Instagram. A long post is
  split into a carousel of numbered cards. Quote posts also get the card on
  Facebook.
- **Videos:** copied as they are (a Reel on Instagram).
- **Instagram captions:** post text, then the save/follow line, a short
  disclaimer and up to 5 hashtags in all (Instagram's limit), matched from
  the post's words. Facebook gets the post text and the disclaimer.
- **Settings** in `config.json`: `ig_footer`, `disclaimer`, `hashtags`
  (keyword → tag, plus general ones), `chart_reels` (which platforms get
  Reels; `[]` turns them off), `frame_charts`, `ig_trial_reels` (set to
  `"SS_PERFORMANCE"` to try Instagram's trial Reels, shown to non-followers
  first; not yet tested), `card_footer`.
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
`main`. Its `card-previews` artifact holds the cards, frames and Reels.

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
