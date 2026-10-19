"""
Ayush's Job Radar v2
--------------------
Finds fresh Azure / Dynatrace / SRE-L1 / Cloud-Ops / Production-Support jobs
from (1) company career-page APIs and (2) LinkedIn + Indeed + Naukri across
many Indian cities, scores them against your skills, removes duplicates and
jobs you already saw, then sends them to Telegram and saves a CSV.

Install:  pip install requests python-jobspy
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional - prints if missing)
Optional: HOURS_OLD (default 168 = 7 days), MAX_EXP_YEARS (default 3),
          FETCH_LINKEDIN_DESC=1 (slower, but better skill matching)
"""

import csv
import html
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

try:
    from jobspy import scrape_jobs
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False

# ==========================================
# 0. SETTINGS
# ==========================================
HOURS_OLD = int(os.environ.get("HOURS_OLD", "168"))          # 7 days
MAX_EXP_YEARS = int(os.environ.get("MAX_EXP_YEARS", "3"))    # skip jobs asking more
FETCH_LINKEDIN_DESC = os.environ.get("FETCH_LINKEDIN_DESC", "0") == "1"
MIN_SKILL_HITS_ATS = 2        # skills needed in full description (career pages)
MIN_SKILL_HITS_PORTAL = 1     # portals often return short/no description
STATE_FILE = "seen_jobs.json"
CSV_FILE = "jobs_found.csv"
STATE_KEEP_DAYS = 30

NOW = datetime.now(timezone.utc)
CUTOFF_TIME = NOW - timedelta(hours=HOURS_OLD)

# ==========================================
# 1. COMPANY CAREER PAGES (ATS APIs)
#    Wrong slugs just return 404 and are skipped (listed at the end).
# ==========================================
GREENHOUSE_COMPANIES = [
    "grafanalabs", "datadog", "newrelic", "elastic", "honeycomb", "chronosphere",
    "logicmonitor", "sumologic", "pagerduty", "cribl", "kentik", "catchpoint",
    "gitlab", "cloudflare", "hashicorp", "digitalocean", "fastly", "netlify",
    "vercel", "circleci", "jfrog", "pulumi", "kong", "chainguard", "spectrocloud",
    "aviatrix", "alkira", "armory", "harness", "buildkite", "octopusdeploy",
    "databricks", "rubrik", "cohesity", "purestorage", "netapp", "couchbase",
    "cockroachlabs", "SingleStore", "neo4j", "redis", "timescale", "planetscale",
    "starburst", "fivetran", "dbt-labs", "airbyte", "astronomer", "confluent",
    "crowdstrike", "zscaler", "okta", "sentinelone", "snyk", "wiz", "orca-security",
    "abnormal-security", "lacework", "illumio", "tanium", "beyondtrust", "varonis",
    "stripe", "twilio", "zendesk", "hubspot", "postman", "hasura", "browserstack",
    "chargebee", "razorpay", "clevertap", "moengage", "freshworks", "druva",
    "innovaccer", "gupshup", "yugabyte", "appsmith", "remote", "deel", "canonical",
    # extra India-heavy / ops-heavy employers
    "phonepe", "groww", "cred", "meesho", "swiggy", "zeta", "juspay", "sharechat",
    "dream11", "unacademy", "upgrad", "pine-labs", "zomato", "paytm", "nutanix",
    "mongodb", "splunk", "dynatrace", "sumo-logic", "thoughtspot", "sprinklr",
]

LEVER_COMPANIES = [
    "rackspace", "veeam", "zimperium", "resilinc", "actian", "kasten",
    "sysdig", "spotinst", "anchore", "solo-io", "buoyant", "linode",
    "scaleway", "percona", "scylladb", "questdb", "crate", "dremio",
    "hevodata", "yellow-ai", "plum", "sprinto", "leena-ai", "lambdatest",
    "cred", "paytm", "meesho", "upgrad", "zeta", "mindtickle", "whatfix",
]

ASHBY_COMPANIES = [
    "vultr", "clickhouse", "supabase", "render", "railway", "fly-io",
    "tailscale", "teleport", "dagger", "earthly", "infisical", "posthog",
    "langfuse", "checkly", "betterstack", "incident-io", "rootly", "firehydrant",
]

# ==========================================
# 2. FILTER RULES
# ==========================================
# Role words that must appear in the TITLE
ROLE_KEYWORDS = [
    "azure", "cloud", "sre", "site reliability", "reliability", "observability",
    "monitoring", "devops", "production support", "application support",
    "infrastructure", "dynatrace", "noc", "platform", "operations",
    "support engineer", "technical support", "systems engineer",
    "system administrator", "sysadmin", "incident", "l1", "l2", "tier 1", "tier 2",
]

# Skills searched in title + description (this is your skill profile)
SKILL_KEYWORDS = [
    "azure", "dynatrace", "monitor", "kql", "log analytics", "application insights",
    "azure monitor", "incident", "observability", "linux", "terraform", "kubernetes",
    "aks", "docker", "prometheus", "grafana", "alert", "troubleshooting", "sla",
    "servicenow", "itil", "splunk", "datadog", "new relic", "ci/cd", "bash",
    "powershell", "root cause", "rca", "on-call", "production support",
]

# Seniority words (matched as whole words, so "head" won't hit "headless")
EXCLUDE_TITLE_PATTERNS = [
    r"\bsenior\b", r"\bsr\b\.?", r"\bstaff\b", r"\bprincipal\b", r"\blead\b",
    r"\bmanager\b", r"\bdirector\b", r"\barchitect\b", r"\bhead\b", r"\bvp\b",
    r"\bvice president\b", r"\bdistinguished\b", r"\biii\b", r"\biv\b",
    r"\bintern(ship)?\b",
]
EXCLUDE_RE = re.compile("|".join(EXCLUDE_TITLE_PATTERNS), re.I)

INDIA_PLACES = [
    "india", "noida", "ghaziabad", "gurgaon", "gurugram", "delhi", "ncr",
    "faridabad", "greater noida", "bangalore", "bengaluru", "hyderabad", "pune",
    "mumbai", "navi mumbai", "thane", "chennai", "kolkata", "ahmedabad",
    "jaipur", "chandigarh", "mohali", "indore", "kochi", "coimbatore",
    "trivandrum", "thiruvananthapuram", "nagpur", "lucknow", "bhubaneswar",
]
REMOTE_OK_WORDS = ["india", "apac", "asia", "anywhere", "worldwide", "global"]


def location_ok(location: str, description: str = "") -> bool:
    loc = location.lower()
    if any(p in loc for p in INDIA_PLACES):
        return True
    if "remote" in loc or "work from home" in loc:
        # global remote: accept only if it clearly welcomes India/APAC
        blob = f"{loc} {description.lower()[:1500]}"
        return any(w in blob for w in REMOTE_OK_WORDS)
    return False


def experience_ok(text: str) -> bool:
    """Reject jobs whose minimum required experience is above MAX_EXP_YEARS."""
    found = re.findall(r"(\d{1,2})\s*(?:\+|-|–|to)?\s*\d{0,2}\s*(?:years|yrs|year)", text.lower())
    if not found:
        return True
    return min(int(x) for x in found) <= MAX_EXP_YEARS


def skill_hits(text: str) -> list:
    t = text.lower()
    return [s for s in SKILL_KEYWORDS if s in t]


def title_ok(title: str) -> bool:
    t = title.lower()
    if EXCLUDE_RE.search(t):
        return False
    return any(r in t for r in ROLE_KEYWORDS)


def strip_html(raw: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ==========================================
# 3. STATE (don't alert the same job twice)
# ==========================================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    cutoff = (NOW - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    return {k: v for k, v in data.items() if v >= cutoff}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


STATE = load_state()
RUN_KEYS = set()
JOBS = []   # every accepted job this run (dicts)


def job_key(company, title, location, link):
    fingerprint = f"{company}|{title}|{location}".lower().strip()
    return fingerprint


def add_job(company, title, location, link, source, posted, skills, score):
    if not link or link == "nan":
        return
    key = job_key(company, title, location, link)
    if key in STATE or key in RUN_KEYS or link in RUN_KEYS:
        return
    RUN_KEYS.add(key)
    RUN_KEYS.add(link)
    JOBS.append({
        "company": company, "title": title, "location": location, "link": link,
        "source": source, "posted": posted.strftime("%Y-%m-%d") if posted else "",
        "skills": ", ".join(skills[:8]), "score": score, "_key": key,
    })


# ==========================================
# 4. CAREER PAGE FETCHERS
# ==========================================
FAILED_SLUGS = []


def _get_json(url, label):
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            FAILED_SLUGS.append(label)
            return None
        return res.json()
    except Exception as e:
        print(f"[Error] {label}: {e}")
        return None


def handle_candidate(company, title, location, desc, link, source, posted):
    if posted and posted < CUTOFF_TIME:
        return
    if not title_ok(title):
        return
    if not location_ok(location, desc):
        return
    full = f"{title} {desc}"
    hits = skill_hits(full)
    if len(hits) < MIN_SKILL_HITS_ATS:
        return
    if not experience_ok(desc):
        return
    add_job(company, title, location, link, source, posted, hits, len(hits))


def greenhouse_one(company):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true",
                     f"greenhouse:{company}")
    for job in (data or {}).get("jobs", []):
        posted = parse_dt(job.get("first_published")) or parse_dt(job.get("updated_at"))
        handle_candidate(company, job.get("title", ""),
                         (job.get("location") or {}).get("name", ""),
                         strip_html(job.get("content", "")),
                         job.get("absolute_url", ""), "Career Page", posted)


def lever_one(company):
    data = _get_json(f"https://api.lever.co/v0/postings/{company}?mode=json", f"lever:{company}")
    for job in data or []:
        posted = datetime.fromtimestamp(job.get("createdAt", 0) / 1000.0, tz=timezone.utc)
        cats = job.get("categories", {}) or {}
        location = f"{cats.get('location', '')} ({job.get('workplaceType', '')})"
        handle_candidate(company, job.get("text", ""), location,
                         job.get("descriptionPlain", ""), job.get("hostedUrl", ""),
                         "Career Page", posted)


def ashby_one(company):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company}", f"ashby:{company}")
    for job in (data or {}).get("jobs", []):
        posted = parse_dt(job.get("publishedAt"))
        rem = "Remote" if job.get("isRemote") else "Onsite/Hybrid"
        location = f"{job.get('location', '')} ({rem})"
        handle_candidate(company, job.get("title", ""), location,
                         job.get("descriptionPlain", ""), job.get("jobUrl", ""),
                         "Career Page", posted)


def fetch_career_pages():
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(greenhouse_one, GREENHOUSE_COMPANIES))
        list(ex.map(lever_one, LEVER_COMPANIES))
        list(ex.map(ashby_one, ASHBY_COMPANIES))


# ==========================================
# 5. PORTAL FETCHER: LinkedIn + Indeed + Naukri, many cities
#    This is what covers "all companies in India", because any company
#    posting on these portals appears here, not just the ones listed above.
# ==========================================
SEARCH_QUERIES = [
    "Azure Dynatrace",
    "Dynatrace engineer",
    "Azure cloud operations engineer",
    "Azure monitoring engineer",
    "Site Reliability Engineer Azure",
    "Associate SRE",
    "Production support engineer Azure",
    "Cloud support engineer Azure",
    "Infrastructure operations engineer",
    "Observability engineer",
]

SEARCH_LOCATIONS = [
    "India", "Noida", "Gurugram", "Delhi NCR", "Bengaluru",
    "Hyderabad", "Pune", "Chennai", "Mumbai",
]


def fetch_portals():
    if not JOBSPY_AVAILABLE:
        print("python-jobspy not installed (pip install python-jobspy). Skipping portals.")
        return

    for query in SEARCH_QUERIES:
        for loc in SEARCH_LOCATIONS:
            try:
                df = scrape_jobs(
                    site_name=["indeed", "linkedin", "naukri"],
                    search_term=query,
                    location=loc,
                    hours_old=HOURS_OLD,
                    results_wanted=30,
                    country_indeed="india",
                    linkedin_fetch_description=FETCH_LINKEDIN_DESC,
                )
            except Exception as e:
                print(f"[JobSpy Error] '{query}' @ {loc}: {e}")
                time.sleep(random.uniform(3, 6))
                continue

            time.sleep(random.uniform(2, 4))   # be gentle, avoid blocks
            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                title = str(row.get("title") or "")
                company = str(row.get("company") or "Company")
                location = str(row.get("location") or loc)
                desc = str(row.get("description") or "")
                if desc == "nan":
                    desc = ""
                site = str(row.get("site") or "portal").capitalize()

                direct, portal = row.get("job_url_direct"), row.get("job_url")
                link = str(direct if direct and str(direct) != "nan" else portal)

                posted = row.get("date_posted")
                posted_dt = None
                if posted is not None and str(posted) not in ("nan", "NaT", "None"):
                    posted_dt = parse_dt(str(posted)[:10] + "T00:00:00+00:00")
                if posted_dt and posted_dt < CUTOFF_TIME - timedelta(days=1):
                    continue

                if not title_ok(title):
                    continue
                if not location_ok(location, desc):
                    continue
                if desc and not experience_ok(desc):
                    continue

                hits = skill_hits(f"{title} {desc}")
                if len(hits) < MIN_SKILL_HITS_PORTAL:
                    continue
                add_job(company, title, location, link, f"via {site}", posted_dt, hits, len(hits))


# ==========================================
# 6. OUTPUT: CSV + TELEGRAM
# ==========================================
def save_csv(jobs):
    fields = ["company", "title", "location", "source", "posted", "score", "skills", "link"]
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(jobs)


def fmt(job):
    e = html.escape
    return (
        f"🏢 <b>{e(job['company'])}</b> ({e(job['source'])})\n"
        f"📌 {e(job['title'])}\n"
        f"🌍 {e(job['location'])}"
        + (f"  |  🗓 {e(job['posted'])}" if job["posted"] else "") + "\n"
        f"🎯 {e(job['skills'])}\n"
        f"🔗 <a href=\"{e(job['link'], quote=True)}\">Apply Here</a>"
    )


def send_telegram(jobs):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    blocks = [fmt(j) for j in jobs]

    if not token or not chat_id:
        print("\n=== MATCHED JOBS ===\n")
        print("\n\n".join(re.sub(r"<[^>]+>", "", b) for b in blocks))
        return

    companies = len({j["company"].lower() for j in jobs})
    header = (f"🚀 <b>Ayush's Job Radar: {len(jobs)} new jobs from {companies} companies "
              f"(last {HOURS_OLD // 24} days)</b>\n\n")
    messages, current = [], header
    for b in blocks:                      # Telegram limit is 4096 chars per message
        if len(current) + len(b) + 2 > 3900:
            messages.append(current)
            current = ""
        current += b + "\n\n"
    messages.append(current)

    for m in messages:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": m, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"Telegram error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Telegram send error: {e}")
        time.sleep(1)


# ==========================================
# 7. MAIN
# ==========================================
if __name__ == "__main__":
    print(f"Window: last {HOURS_OLD}h | max experience: {MAX_EXP_YEARS} yrs")

    total_slugs = len(GREENHOUSE_COMPANIES) + len(LEVER_COMPANIES) + len(ASHBY_COMPANIES)
    print(f"Scanning {total_slugs} company career pages...")
    fetch_career_pages()
    print(f"  career-page matches: {len(JOBS)}")

    print(f"Scanning LinkedIn/Indeed/Naukri: {len(SEARCH_QUERIES)} queries x "
          f"{len(SEARCH_LOCATIONS)} locations (this takes a while)...")
    fetch_portals()

    JOBS.sort(key=lambda j: j["score"], reverse=True)   # best skill match first
    print(f"Total new matches: {len(JOBS)} from "
          f"{len({j['company'].lower() for j in JOBS})} companies")

    if FAILED_SLUGS:
        print(f"Career-page slugs not found (fix or remove): {', '.join(sorted(FAILED_SLUGS))}")

    if JOBS:
        save_csv(JOBS)
        send_telegram(JOBS)
        for j in JOBS:
            STATE[j["_key"]] = NOW.isoformat()
        save_state(STATE)
    else:
        print("No new matching jobs.")
