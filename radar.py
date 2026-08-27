"""
newgrad-radar
=============
Finds brand-new new-grad roles in Data Science / AI / ML and posts the best 5
to a Discord channel.

The pipeline has five stages. Read them in order -- each one is a labelled
section below.

    1. FETCH      pull ~19,000 listings from a public GitHub JSON file
    2. PREFILTER  cheap Python rules cut that to ~50 candidates (no AI, free)
    3. CLASSIFY   one Claude call judges the survivors (the only AI step)
    4. DEDUPE     drop anything we already sent you
    5. NOTIFY     post the top 5 to Discord

Run it locally:      python radar.py
Preview without posting or saving:   python radar.py --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG -- tune these, this is the part you'll actually edit
# ---------------------------------------------------------------------------

# Where the job data comes from. This file is regenerated continuously by a
# GitHub Action in the SimplifyJobs repo, so it's always fresh.
LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)

# Only consider jobs posted within this many days.
MAX_AGE_DAYS = 7

# How many jobs to put in one Discord message.
JOBS_PER_RUN = 5

# The upstream data already tags each job with a category. We only want these.
WANTED_CATEGORIES = {"AI/ML/Data", "Data Science, AI & Machine Learning"}

# A title must match at least one of these to survive the prefilter.
# `\b` means "word boundary" so "ai" won't match inside "chair".
TITLE_INCLUDE = re.compile(
    r"\b("
    r"data scien(ce|tist)|machine learning|ml engineer|\bai\b|"
    r"a\.?i\.?/ml|artificial intelligence|gen ?ai|generative ai|"
    r"deep learning|nlp|computer vision|llm|applied scien(ce|tist)|"
    r"research engineer|mlops|data engineer"
    r")",
    re.IGNORECASE,
)

# If a title matches ANY of these, throw it out. Cheaper than asking Claude.
TITLE_EXCLUDE = re.compile(
    r"\b("
    r"senior|staff|principal|lead\b|manager|director|head of|vp\b|"
    r"intern(ship)?\b|co-?op\b|contract|part.time|"
    r"phd required|professor|faculty|postdoc"
    r")",
    re.IGNORECASE,
)

# Anthropic model used for the classify step. Haiku is the cheap, fast one --
# this is a sorting task, not a reasoning task, so it's the right call.
MODEL = "claude-haiku-4-5-20251001"

# File where we remember what we've already sent. This is what stops you
# getting the same five jobs every single morning.
SEEN_PATH = Path(__file__).parent / "state" / "seen.json"

# Discord caps a message at 10 embeds. We're under it, but don't raise
# JOBS_PER_RUN past 10 without batching.
DISCORD_EMBED_LIMIT = 10


# ---------------------------------------------------------------------------
# STAGE 1 -- FETCH
# ---------------------------------------------------------------------------

def fetch_listings():
    """Download the listings file and return it as a list of dicts."""
    print(f"[1/5] fetching {LISTINGS_URL}")
    req = urllib.request.Request(
        LISTINGS_URL,
        # Some CDNs are picky about requests with no User-Agent.
        headers={"User-Agent": "newgrad-radar"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    print(f"      got {len(data):,} total listings")
    return data


# ---------------------------------------------------------------------------
# STAGE 2 -- PREFILTER  (pure Python, zero cost)
# ---------------------------------------------------------------------------

def prefilter(listings):
    """
    Cut ~19,000 listings down to a few dozen using cheap rules.

    The whole point of this stage is cost. Sending 19,000 job titles to an
    LLM would be slow and expensive. Sending 50 costs a fraction of a cent.
    Do as much as you can with free rules, then let the model handle the
    genuinely ambiguous cases.
    """
    print("[2/5] prefiltering")
    cutoff = time.time() - (MAX_AGE_DAYS * 86400)
    out = []

    for job in listings:
        # Skip anything the upstream repo has marked dead or hidden.
        if not job.get("active") or not job.get("is_visible"):
            continue
        # Skip anything older than our window.
        if job.get("date_posted", 0) < cutoff:
            continue
        # Skip categories we don't care about.
        if job.get("category") not in WANTED_CATEGORIES:
            continue

        title = job.get("title", "")
        if TITLE_EXCLUDE.search(title):
            continue
        if not TITLE_INCLUDE.search(title):
            continue

        out.append(job)

    # Newest first, so if we have to truncate we keep the freshest.
    out.sort(key=lambda j: j.get("date_posted", 0), reverse=True)
    print(f"      {len(out)} candidates survived")
    return out


# ---------------------------------------------------------------------------
# STAGE 3 -- CLASSIFY  (the one AI step)
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """You are screening job postings for a new graduate who \
wants roles in Data Science, AI Engineering, Gen AI Engineering, or AI/ML.

Below is a numbered list of job titles with their companies. For each one, \
decide whether it is genuinely a NEW GRAD / entry-level role in one of those \
areas.

Reject a posting if any of these are true:
- it requires meaningful prior industry experience (2+ years)
- it is not really an AI/ML/data role (e.g. geoscientist, business analyst, \
policy researcher, general software engineering with no ML component)
- it is a senior, lead, or managerial position
- it is an internship rather than a full-time role

Return ONLY a JSON array, no prose and no markdown fences. One object per \
posting you ACCEPT (skip rejects entirely):

[{"n": <the number from the list>, "score": <0-100 fit>, "why": "<max 12 \
words on why it fits>"}]

Score higher for: explicit "new grad"/"university graduate"/"early career" \
framing, and closer match to Data Science / AI Engineer / Gen AI / ML \
Engineer specifically.

Postings:
"""


def classify(candidates):
    """
    Ask Claude which candidates are really new-grad AI/ML roles.

    If there's no API key set, this degrades gracefully: it just returns
    everything sorted by recency. That means you can run the whole pipeline
    on day one without signing up for anything.
    """
    print("[3/5] classifying")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("      no ANTHROPIC_API_KEY set -- skipping AI step, "
              "ranking by recency instead")
        return [dict(job, _score=50, _why="unscored") for job in candidates]

    try:
        import anthropic
    except ImportError:
        print("      `anthropic` package not installed -- skipping AI step")
        return [dict(job, _score=50, _why="unscored") for job in candidates]

    # Build the numbered list. We send only title + company + location --
    # not the whole record -- to keep the prompt small.
    lines = []
    for i, job in enumerate(candidates):
        locs = ", ".join(job.get("locations", [])[:2]) or "n/a"
        lines.append(f'{i}. {job["company_name"]} — {job["title"]} ({locs})')
    prompt = CLASSIFY_PROMPT + "\n".join(lines)

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    # Pull the text out of the response and parse it.
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Models sometimes wrap JSON in ```json fences despite instructions.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        verdicts = json.loads(text)
    except json.JSONDecodeError:
        print("      couldn't parse model output -- falling back to recency")
        print(f"      raw output was: {text[:200]}")
        return [dict(job, _score=50, _why="unscored") for job in candidates]

    accepted = []
    for v in verdicts:
        idx = v.get("n")
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            continue  # model hallucinated an index; ignore it
        accepted.append(dict(
            candidates[idx],
            _score=v.get("score", 0),
            _why=v.get("why", ""),
        ))

    accepted.sort(key=lambda j: j["_score"], reverse=True)
    print(f"      model accepted {len(accepted)} of {len(candidates)}")
    return accepted


# ---------------------------------------------------------------------------
# STAGE 4 -- DEDUPE
# ---------------------------------------------------------------------------

def load_seen():
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file from growing forever -- 5,000 IDs is plenty of history.
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-5000:], indent=0))


def dedupe(jobs, seen):
    print("[4/5] deduping")
    fresh = [j for j in jobs if j["id"] not in seen]
    print(f"      {len(fresh)} unseen (filtered out {len(jobs) - len(fresh)})")
    return fresh


# ---------------------------------------------------------------------------
# STAGE 5 -- NOTIFY
# ---------------------------------------------------------------------------

def post_to_discord(jobs, webhook_url):
    """
    Send jobs as Discord 'embeds' -- the nice card-shaped messages with a
    coloured bar down the left side.
    """
    print("[5/5] posting to Discord")

    embeds = []
    for job in jobs[:DISCORD_EMBED_LIMIT]:
        locs = ", ".join(job.get("locations", [])[:3]) or "Location n/a"
        age_h = int((time.time() - job.get("date_posted", 0)) / 3600)
        why = job.get("_why", "")
        embeds.append({
            "title": job["title"][:250],
            "url": job["url"],
            "description": f"**{job['company_name']}**\n{locs}",
            "color": 0x5865F2,
            "footer": {
                "text": f"posted {age_h}h ago"
                        + (f" · {why}" if why and why != "unscored" else "")
            },
        })

    payload = {
        "content": f"**{len(embeds)} new grad AI/ML roles** "
                   f"· {time.strftime('%a %b %d')}",
        "embeds": embeds,
    }

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # Discord is behind Cloudflare, which 403s Python's default
            # "Python-urllib/3.x" User-Agent. Any real-looking string works.
            "User-Agent": "newgrad-radar/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Discord returns 204 No Content on success.
        print(f"      Discord responded {resp.status}")


# ---------------------------------------------------------------------------
# ORCHESTRATOR
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print results instead of posting; don't touch seen.json")
    args = ap.parse_args()

    listings = fetch_listings()
    candidates = prefilter(listings)

    if not candidates:
        print("nothing survived the prefilter -- exiting quietly")
        return 0

    # Cap what we send to the model. 60 titles is a small, cheap prompt.
    scored = classify(candidates[:60])

    seen = load_seen()
    fresh = dedupe(scored, seen)

    if not fresh:
        print("no new jobs since last run -- exiting quietly")
        return 0

    batch = fresh[:JOBS_PER_RUN]

    if args.dry_run:
        print("\n--- DRY RUN, would have posted: ---")
        for j in batch:
            print(f"  [{j.get('_score')}] {j['company_name']} — {j['title']}")
            print(f"        {j['url']}")
        return 0

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        return 1

    post_to_discord(batch, webhook)

    # Only mark jobs as seen AFTER a successful post. If Discord errors out,
    # we crash above and these stay unseen, so tomorrow's run retries them.
    seen.update(j["id"] for j in batch)
    save_seen(seen)
    print(f"done -- sent {len(batch)}, remembered {len(seen)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
