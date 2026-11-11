# dealsteal setup

Everything the README leaves out: how it works, how to configure it, and how to
run it locally.

```
cron-job.org (hourly)
        │  POST /repos/spartanjax/dealsteal/dispatches
        ▼
GitHub Actions (.github/workflows/dealsteal.yml)
        │  restores the state branch, then runs main.py
        ▼
main.py
        ├─ fetch each wishlist item from the Trade Me API
        ├─ drop listings already in SQLite  (the "unseen" filter)
        ├─ apply hard rules                 (price, region, seller, keywords)
        ├─ judge the survivors with DeepSeek
        ├─ one ntfy notification if any are deals
        └─ push the updated SQLite database back to the state branch
```

## Why a state branch

GitHub Actions runners are ephemeral, so "have I seen this listing before?"
needs somewhere durable to live. The database is committed to an orphan
branch called `state`, force-pushed as a single commit each run. No external
service, no cache eviction, and the listing history stays auditable.

If a run finds nothing new it exits before touching the LLM or ntfy, and does
not push state. Quiet hours cost nothing.

## Setup

### 1. Trade Me API application

Register an app at
<https://developer.trademe.co.nz/api-overview/registering-an-application>.
Search is a *protected* endpoint, so you need all four credentials. The
easiest route is the token generator at
<https://developer.trademe.co.nz/api-overview/authentication>: enter your
consumer key and secret, tick `MyTradeMeRead`, and it returns an OAuth token
and token secret immediately.

| Secret | Where it comes from |
| --- | --- |
| `TRADEME_CONSUMER_KEY` | your registered application |
| `TRADEME_CONSUMER_SECRET` | your registered application |
| `TRADEME_TOKEN` | the token generator form |
| `TRADEME_TOKEN_SECRET` | the token generator form |

Tokens are deleted after six months of no use, and stop working if you change
your Trade Me password.

### 2. DeepSeek key

`DEEPSEEK_API_KEY` from <https://platform.deepseek.com>. One `deepseek-flash`
call per wishlist item per run, capped at 25 listings, so cost is fractions of
a cent.

### 3. ntfy topic

`NTFY_TOPIC` is the topic *name* only, not the URL. Subscribe to the same
topic in the ntfy app to get the pushes.

### 4. Add the secrets

```bash
gh secret set TRADEME_CONSUMER_KEY    -R spartanjax/dealsteal
gh secret set TRADEME_CONSUMER_SECRET -R spartanjax/dealsteal
gh secret set TRADEME_TOKEN           -R spartanjax/dealsteal
gh secret set TRADEME_TOKEN_SECRET    -R spartanjax/dealsteal
gh secret set DEEPSEEK_API_KEY        -R spartanjax/dealsteal
gh secret set NTFY_TOPIC              -R spartanjax/dealsteal
```

Each command prompts for the value, so nothing lands in your shell history.

### 5. Hourly trigger

GitHub's own `on: schedule` is unreliable, so the timing is owned by
cron-job.org:

- **URL:** `https://api.github.com/repos/spartanjax/dealsteal/dispatches`
- **Method:** `POST`
- **Schedule:** hourly
- **Headers:**
  - `Authorization: Bearer <FINE_GRAINED_PAT>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2026-03-10`
  - `Content-Type: application/json`
- **Body:** `{"event_type":"dealsteal-run"}`

The PAT needs **Contents: Read and write** on this repo. Give it an expiry and
rotate before it lapses, otherwise the hourly run silently stops. Enable
cron-job.org failure notifications so an expired token is loud.

## Wishlist

Edit `config/wishlist.yaml`. One entry per thing you are hunting:

```yaml
items:
  - name: standing desk
    query: standing desk        # Trade Me search string
    max_price: 400              # applied client-side; the API has no price filter
    min_price: 60
    regions: [Auckland]
    exclude_terms: [wanted, "parts only", faulty]
    must_match_any: [desk]
    condition: used
```

`seller_status` defaults to `casual`, which asks Trade Me for individual
sellers only, so shop and retailer stock never reaches the judge. Set it to
`in_trade` or `all` per item if you ever want the opposite.

`config/criteria.md` is the judge's brief. It is plain prose, edited like a
note to a friend: what counts as a deal, what is a hard pass, what deserves a
red flag. Changing it changes behaviour with no code change.

## Local runs

```bash
uv venv .venv && uv pip install -r requirements.txt
cp .env.example .env          # fill in the six values
.venv/bin/python main.py --dry-run
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--dry-run` | Fetch and judge, print the notification, send and save nothing |
| `--item NAME` | Only run one wishlist item |
| `--fixture PATH` | Replay a recorded Trade Me response instead of calling the API |
| `--no-llm` | Skip the judge, report counts only |
| `--no-state` | Do not restore or push the state branch |

`tests/fixtures/trademe_deal.json` is a recorded response with one obvious
bargain and three distractors, handy for exercising the judge without live
credentials:

```bash
.venv/bin/python main.py --fixture tests/fixtures/trademe_deal.json --dry-run
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

## Reading the database

```bash
git fetch origin state
git show FETCH_HEAD:data/dealsteal.db > /tmp/dealsteal.db
sqlite3 /tmp/dealsteal.db "select item, count(*) from listings group by item;"
```

`verdicts` holds every judgement with its reason, so you can check whether the
judge is being too generous without re-running anything.

## Behaviour worth knowing

- **Dedupe is per wishlist item.** A listing that matches two items is tracked
  separately for each, so changing the wishlist never suppresses an alert you
  already received.
- **The first run notifies on everything.** The database starts empty, so use
  `--dry-run` once before enabling the hourly cron.
- **Prices are appended to history only when they change**, which is what makes
  the "recent median" comparison meaningful over time.
- **Only the first 25 new listings per item reach the LLM.** The rest are
  recorded as seen and reported as "held back", so a too-broad query is visible
  rather than silently expensive.
- **A failed judge still notifies**, at low priority, so an outage does not
  fail silently.
