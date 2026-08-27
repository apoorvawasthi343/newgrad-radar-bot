# newgrad-radar

Posts 5 fresh new-grad Data Science / AI / ML job openings to a Discord
channel every weekday morning. Runs on GitHub Actions, so your laptop can be
closed.

```
19,085 listings  →  prefilter  →  ~40  →  Claude  →  ranked  →  dedupe  →  5 in Discord
```

---

## Setup

Do these in order. Steps 1-2 get it working locally; steps 3-5 put it on
autopilot.

### 1. Get it running locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python radar.py --dry-run
```

`--dry-run` prints what it *would* send without posting anything or touching
`seen.json`. You should see 5 jobs. **Get this working before anything else.**

If PowerShell blocks the activate script, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 2. Make a Discord webhook

A webhook is a secret URL. Anything you POST to it shows up as a message in
that channel. No bot account, no OAuth, no permissions to configure.

1. Make a Discord server (you can make one just for yourself — it's free)
2. Right-click your channel → **Edit Channel** → **Integrations** → **Webhooks**
3. **New Webhook** → **Copy Webhook URL**

Test it locally:

```powershell
$env:DISCORD_WEBHOOK_URL = "paste-your-url-here"
python radar.py
```

Check Discord. You should have 5 job cards.

> Treat that URL like a password. Anyone who has it can post to your channel.
> Never commit it to the repo.

### 3. Add the Claude API key (optional but recommended)

Without a key the script still works — it just ranks by recency instead of
relevance, and you'll get some noise (geoscientists, business analysts).

Get a key at [platform.claude.com](https://platform.claude.com), then:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python radar.py --dry-run
```

Cost is negligible — you're sending ~40 job titles to Haiku once per day.

### 4. Push to GitHub

```powershell
git init
git add .
git commit -m "initial commit"
gh repo create newgrad-radar --private --source=. --push
```

(Or make the repo on github.com and follow the instructions it gives you.)

### 5. Add your secrets and turn it on

On github.com, go to your repo → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**. Add two:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | your webhook URL |
| `ANTHROPIC_API_KEY` | your API key |

Then go to the **Actions** tab → **New Grad Radar** → **Run workflow**. That
button is why `workflow_dispatch` is in the workflow file — don't wait until
tomorrow to find out you have a typo.

---

## Tuning it

Everything you'll want to change is in the `CONFIG` block at the top of
`radar.py`:

- **Getting too few jobs?** Raise `MAX_AGE_DAYS`, or loosen `TITLE_INCLUDE`.
- **Getting junk?** Add words to `TITLE_EXCLUDE`, or tighten the reject rules
  in `CLASSIFY_PROMPT`. The prompt is plain English — just edit it.
- **Wrong time?** `cron` in the workflow is **UTC**. `0 13` = 9am Eastern
  during summer, 8am during winter. GitHub won't adjust for daylight saving.
- **Want to start over?** Empty `state/seen.json` back to `[]`.

## Gotchas

- **Scheduled runs are best-effort.** GitHub delays cron jobs when its
  infrastructure is busy, sometimes by 15+ minutes. This is normal.
- **GitHub disables schedules on inactive repos** after ~60 days without
  commits. Since this one commits `seen.json` on every run, it keeps itself
  alive.
- **Jobs are only marked "seen" after a successful Discord post.** If Discord
  is down, the script crashes and those jobs get retried tomorrow instead of
  being silently lost.

## Where to take it next

The single source here covers a lot, but it's one source. The natural upgrade
is adding company job boards directly — these are free, public, no-auth JSON
APIs:

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{company}/jobs`
- Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{company}`

Write a `companies.yaml`, add a second fetcher, merge the results before the
prefilter stage. Everything downstream keeps working unchanged — which is the
whole reason the pipeline is split into stages.
