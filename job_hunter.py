"""
Ayush's Job Radar v3  -  level-aware matching
=============================================
Only keeps jobs that fit YOUR level (~1 year, Azure + Dynatrace + monitoring/RCA)
and shows, for every job:
   * match % and tier (A = apply now, B = good fit / small stretch)
   * experience the job asks for
   * which of your skills match
   * which required technologies you DON'T have yet (your skill gaps)
   * work mode (Remote / Hybrid / Onsite)

Install:  pip install requests python-jobspy
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional - prints if missing)
Tuning  : HOURS_OLD=168  MY_EXP_YEARS=1  REMOTE_ONLY=0  MIN_MATCH_PCT=40
          MAX_ALERTS=30  FETCH_LINKEDIN_DESC=1
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
HOURS_OLD = int(os.environ.get("HOURS_OLD", "168"))
MY_EXP_YEARS = int(os.environ.get("MY_EXP_YEARS", "1"))
MAX_MIN_EXP = MY_EXP_YEARS + 2          # job asking more than this is rejected
REMOTE_ONLY = os.environ.get("REMOTE_ONLY", "0") == "1"
MIN_MATCH_PCT = int(os.environ.get("MIN_MATCH_PCT", "40"))
MAX_ALERTS = int(os.environ.get("MAX_ALERTS", "30"))
FETCH_LINKEDIN_DESC = os.environ.get("FETCH_LINKEDIN_DESC", "1") == "1"
TARGET_SCORE = 16                        # score that equals 100% match
STATE_FILE, CSV_FILE, STATE_KEEP_DAYS = "seen_jobs.json", "jobs_found.csv", 30

NOW = datetime.now(timezone.utc)
CUTOFF_TIME = NOW - timedelta(hours=HOURS_OLD)

# ==========================================
# 1. YOUR SKILL PROFILE (weights = how much it matters)
# ==========================================
CORE_SKILLS = {
    "dynatrace": 4, "azure": 3, "azure monitor": 3, "log analytics": 3,
    "application insights": 3, "kql": 3, "aks": 2, "observability": 2,
    "monitoring": 2, "incident": 2, "root cause": 2, "rca": 2, "slo": 2,
    "synthetic": 2, "event hub": 2, "iot hub": 2, "azure devops": 2,
    "alerting": 1, "sla": 1, "key vault": 1, "service bus": 1, "smartscape": 2,
}
SUPPORT_SKILLS = {
    "kubernetes": 1, "docker": 1, "linux": 1, "terraform": 1, "grafana": 1,
    "prometheus": 1, "ci/cd": 1, "git": 1, "python": 1, "bash": 1,
    "powershell": 1, "servicenow": 1, "itil": 1, "datadog": 1, "new relic": 1,
    "splunk": 1, "helm": 1, "bicep": 1, "infrastructure as code": 1,
}
# A job must mention at least one of these (your real strengths)
ANCHORS = ["azure", "dynatrace", "application insights", "log analytics", "kql"]

# Technologies you do NOT have. Shown as "gaps" and they reduce the match score.
GAP_TECH = [
    "aws", "gcp", "google cloud", "java", "golang", "c#", ".net", "windows server",
    "oracle", "sql server", "kafka", "elasticsearch", "jenkins", "puppet", "chef",
    "openshift", "vmware", "ansible", "nginx", "ruby", "scala", "php",
]

# ==========================================
# 2. TITLE RULES
# ==========================================
TARGET_RE = re.compile(
    r"\bsre\b|site reliability|reliability engineer|azure|cloud (engineer|operations|ops|support|infrastructure|associate|analyst)"
    r"|devops|observability|monitoring|dynatrace|production support|application support"
    r"|platform (engineer|support)|infrastructure (engineer|operations|support)", re.I)

SENIOR_RE = re.compile(
    r"\bsenior\b|\bsr\b\.?|\bstaff\b|\bprincipal\b|\blead\b|\bmanager\b|\bdirector\b|\barchitect\b"
    r"|\bhead\b|\bvp\b|\bvice president\b|\bdistinguished\b|\biii\b|\biv\b|\bintern(ship)?\b|\bconsultant\b", re.I)

# Always rejected (these were the irrelevant Naukri/NOC results)
NEG_STRICT_RE = re.compile(
    r"developer|full ?stack|front ?end|back ?end|data (scientist|analyst|engineer)|machine learning|\bml\b"
    r"|\bai\b|security|soc analyst|\bqa\b|tester|sales|customer|service desk|help ?desk|desktop"
    r"|\bnoc\b|\bdba\b|database admin|windows|network engineer|\bsap\b|recruit|\bhr\b", re.I)
# Rejected unless the title also has a cloud/SRE word
NEG_SOFT_RE = re.compile(r"software engineer|\bsde\b|technical support|support engineer", re.I)
SOFT_EXCEPTION_RE = re.compile(r"\bsre\b|site reliability|azure|dynatrace|devops|cloud", re.I)

JUNIOR_RE = re.compile(r"\b(associate|junior|jr|entry|fresher|trainee|engineer i|l1|l2|graduate)\b", re.I)
LEVEL_II_RE = re.compile(r"\b(ii|2)\s*$|\b(ii|2)\b", re.I)

INDIA_PLACES = [
    "india", "noida", "ghaziabad", "gurgaon", "gurugram", "delhi", "ncr", "faridabad",
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "thane", "chennai",
    "kolkata", "ahmedabad", "jaipur", "chandigarh", "mohali", "indore", "kochi",
    "coimbatore", "trivandrum", "thiruvananthapuram", "nagpur", "lucknow", "bhubaneswar",
]
REMOTE_OK_WORDS = ["india", "apac", "asia", "anywhere", "worldwide", "global"]


# ==========================================
# 3. HELPERS
# ==========================================
def contains(term: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


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


def location_ok(location: str, description: str = "") -> bool:
    loc = location.lower()
    if any(p in loc for p in INDIA_PLACES):
        return True
    if "remote" in loc or "work from home" in loc:
        return any(w in f"{loc} {description.lower()[:1500]}" for w in REMOTE_OK_WORDS)
    return False


def work_mode(location: str, desc: str, remote_flag=None) -> str:
    blob = f"{location} {desc[:1500]}".lower()
    if remote_flag is True or "remote" in blob or "work from home" in blob or "wfh" in blob:
        if "hybrid" in blob and "fully remote" not in blob:
            return "Hybrid"
        return "Remote"
    if "hybrid" in blob:
        return "Hybrid"
    return "Onsite"


def parse_experience(text: str, trust: bool = False):
    """Returns (min_years, max_years_or_None) or None. trust=True skips the
    'experience' context check (for fields that are already experience ranges)."""
    text = (text or "").lower()
    patterns = [
        (r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", True),
        (r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", False),
        (r"(\d{1,2})\s*(?:years?|yrs?)", False),
    ]
    used, found = [], []
    for pat, is_range in patterns:
        for m in re.finditer(pat, text):
            s, e = m.span()
            if any(s < ue and e > us for us, ue in used):
                continue
            used.append((s, e))
            window = text[max(0, s - 80): e + 80]
            if not trust and "exp" not in window:
                continue
            found.append((int(m.group(1)), int(m.group(2)) if is_range else None))
    if not found:
        return None
    return max(found, key=lambda c: c[0])   # the strictest requirement mentioned


def fmt_exp(exp) -> str:
    if not exp:
        return "Not stated"
    lo, hi = exp
    return f"{lo}–{hi} yrs" if hi is not None else f"{lo}+ yrs"


# ==========================================
# 4. STATE (never alert the same job twice)
# ==========================================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    cutoff = (NOW - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    return {k: v for k, v in data.items() if v >= cutoff}


STATE = load_state()
RUN_KEYS = set()
JOBS = []
FAILED_SLUGS = []


# ==========================================
# 5. THE MATCHER (one place that decides everything)
# ==========================================
def evaluate(company, title, location, desc, link, source, posted,
             exp_hint="", level_hint="", remote_flag=None):
    if not link or link == "nan":
        return
    if posted and posted < CUTOFF_TIME:
        return

    tl = title.lower()
    if SENIOR_RE.search(tl) or NEG_STRICT_RE.search(tl):
        return
    if NEG_SOFT_RE.search(tl) and not SOFT_EXCEPTION_RE.search(tl):
        return
    if not TARGET_RE.search(tl):
        return
    if not location_ok(location, desc):
        return

    mode = work_mode(location, desc, remote_flag)
    if REMOTE_ONLY and mode != "Remote":
        return

    blob = f"{tl} {desc.lower()}"
    if not any(contains(a, blob) for a in ANCHORS):
        return

    # ---- experience gate ----
    exp = parse_experience(exp_hint, trust=True) or parse_experience(desc)
    tier_cap = "A"
    if exp:
        if exp[0] > MAX_MIN_EXP:
            return                       # needs more than you have -> skip
        if exp[0] == MAX_MIN_EXP:
            tier_cap = "B"               # small stretch
    else:
        if not JUNIOR_RE.search(tl):
            tier_cap = "B"               # unknown experience -> not "apply now"
    if LEVEL_II_RE.search(tl) and not exp:
        tier_cap = "B"
    if "mid-senior" in (level_hint or "").lower() and not exp:
        tier_cap = "B"

    # ---- skill score ----
    core = [s for s in CORE_SKILLS if contains(s, blob)]
    support = [s for s in SUPPORT_SKILLS if contains(s, blob)]
    gaps = [g for g in GAP_TECH if contains(g, blob)]
    score = sum(CORE_SKILLS[s] for s in core) + sum(SUPPORT_SKILLS[s] for s in support)
    score -= min(6, 2 * len(gaps))
    pct = max(0, min(100, int(score / TARGET_SCORE * 100)))
    if pct < MIN_MATCH_PCT:
        return
    tier = "A" if (pct >= 70 and tier_cap == "A") else "B"

    key = f"{company}|{title}|{location}".lower().strip()
    if key in STATE or key in RUN_KEYS or link in RUN_KEYS:
        return
    RUN_KEYS.update({key, link})
    JOBS.append({
        "tier": tier, "pct": pct, "company": company, "title": title,
        "location": location, "mode": mode, "exp": fmt_exp(exp),
        "matched": ", ".join(core + support), "gaps": ", ".join(gaps),
        "source": source, "posted": posted.strftime("%Y-%m-%d") if posted else "",
        "link": link, "_key": key,
    })


# ==========================================
# 6. COMPANY CAREER PAGES (ATS APIs)
# ==========================================
GREENHOUSE_COMPANIES = [
    "grafanalabs", "datadog", "newrelic", "elastic", "honeycomb", "chronosphere",
    "logicmonitor", "sumologic", "pagerduty", "cribl", "kentik", "catchpoint",
    "gitlab", "cloudflare", "hashicorp", "digitalocean", "fastly", "netlify",
    "vercel", "circleci", "jfrog", "pulumi", "kong", "spectrocloud", "harness",
    "databricks", "rubrik", "cohesity", "purestorage", "netapp", "couchbase",
    "cockroachlabs", "redis", "confluent", "crowdstrike", "zscaler", "okta",
    "stripe", "twilio", "zendesk", "hubspot", "postman", "browserstack",
    "chargebee", "razorpay", "clevertap", "moengage", "freshworks", "druva",
    "innovaccer", "gupshup", "canonical", "phonepe", "groww", "cred", "meesho",
    "swiggy", "zeta", "juspay", "sharechat", "dream11", "upgrad", "pine-labs",
    "nutanix", "mongodb", "dynatrace", "thoughtspot", "sprinklr",
]
LEVER_COMPANIES = [
    "rackspace", "veeam", "sysdig", "percona", "scylladb", "hevodata", "yellow-ai",
    "plum", "sprinto", "leena-ai", "lambdatest", "cred", "paytm", "meesho",
    "upgrad", "zeta", "mindtickle", "whatfix",
]
ASHBY_COMPANIES = [
    "vultr", "clickhouse", "supabase", "render", "tailscale", "teleport",
    "infisical", "posthog", "checkly", "betterstack", "incident-io", "rootly",
]


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


def greenhouse_one(company):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true",
                     f"greenhouse:{company}")
    for j in (data or {}).get("jobs", []):
        posted = parse_dt(j.get("first_published")) or parse_dt(j.get("updated_at"))
        evaluate(company, j.get("title", ""), (j.get("location") or {}).get("name", ""),
                 strip_html(j.get("content", "")), j.get("absolute_url", ""),
                 "Career Page", posted)


def lever_one(company):
    data = _get_json(f"https://api.lever.co/v0/postings/{company}?mode=json", f"lever:{company}")
    for j in data or []:
        posted = datetime.fromtimestamp(j.get("createdAt", 0) / 1000.0, tz=timezone.utc)
        cats = j.get("categories", {}) or {}
        evaluate(company, j.get("text", ""),
                 f"{cats.get('location', '')} ({j.get('workplaceType', '')})",
                 j.get("descriptionPlain", ""), j.get("hostedUrl", ""), "Career Page", posted)


def ashby_one(company):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company}", f"ashby:{company}")
    for j in (data or {}).get("jobs", []):
        rem = "Remote" if j.get("isRemote") else "Onsite/Hybrid"
        evaluate(company, j.get("title", ""), f"{j.get('location', '')} ({rem})",
                 j.get("descriptionPlain", ""), j.get("jobUrl", ""), "Career Page",
                 parse_dt(j.get("publishedAt")), remote_flag=bool(j.get("isRemote")))


def fetch_career_pages():
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(greenhouse_one, GREENHOUSE_COMPANIES))
        list(ex.map(lever_one, LEVER_COMPANIES))
        list(ex.map(ashby_one, ASHBY_COMPANIES))


# ==========================================
# 7. PORTALS: LinkedIn + Indeed + Naukri (covers every company posting there)
# ==========================================
SEARCH_QUERIES = [
    "Azure Dynatrace",
    "Dynatrace engineer",
    "Azure monitoring engineer",
    "Azure cloud support engineer",
    "Associate Site Reliability Engineer Azure",
    "Cloud operations engineer Azure",
    "Azure DevOps engineer",
    "Observability engineer",
    "Azure cloud engineer",
]
# (location, remote_only_search)
SEARCH_PLAN = [("India", True), ("India", False), ("Noida", False), ("Gurugram", False),
               ("Bengaluru", False), ("Hyderabad", False), ("Pune", False), ("Chennai", False)]


def _clean(v, default=""):
    s = str(v) if v is not None else default
    return default if s in ("nan", "None", "NaT") else s


def fetch_portals():
    if not JOBSPY_AVAILABLE:
        print("python-jobspy not installed (pip install python-jobspy). Skipping portals.")
        return
    for query in SEARCH_QUERIES:
        for loc, remote in SEARCH_PLAN:
            try:
                df = scrape_jobs(
                    site_name=["indeed", "linkedin", "naukri"],
                    search_term=query, location=loc, is_remote=remote,
                    hours_old=HOURS_OLD, results_wanted=25, country_indeed="india",
                    linkedin_fetch_description=FETCH_LINKEDIN_DESC,
                )
            except Exception as e:
                print(f"[JobSpy Error] '{query}' @ {loc}: {e}")
                time.sleep(random.uniform(3, 6))
                continue
            time.sleep(random.uniform(2, 4))
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                desc = _clean(row.get("description"))
                extra_skills = _clean(row.get("skills"))          # Naukri tag list
                if extra_skills:
                    desc = f"{desc} {extra_skills}"
                direct, portal = _clean(row.get("job_url_direct")), _clean(row.get("job_url"))
                posted = parse_dt(_clean(row.get("date_posted"))[:10] + "T00:00:00+00:00") \
                    if _clean(row.get("date_posted")) else None
                evaluate(
                    _clean(row.get("company"), "Company"), _clean(row.get("title")),
                    _clean(row.get("location"), loc), desc, direct or portal,
                    f"via {_clean(row.get('site'), 'portal').capitalize()}", posted,
                    exp_hint=_clean(row.get("experience_range")),
                    level_hint=_clean(row.get("job_level")),
                    remote_flag=True if row.get("is_remote") is True else None,
                )


# ==========================================
# 8. OUTPUT
# ==========================================
def save_csv(jobs):
    fields = ["tier", "pct", "company", "title", "location", "mode", "exp",
              "matched", "gaps", "source", "posted", "link"]
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(jobs)


def fmt(j):
    e = html.escape
    icon = {"Remote": "🏠", "Hybrid": "🔀", "Onsite": "🏢"}[j["mode"]]
    tier = "🟢 A – apply now" if j["tier"] == "A" else "🟡 B – good fit / small stretch"
    lines = [
        f"{tier}  |  {j['pct']}% match",
        f"🏢 <b>{e(j['company'])}</b> ({e(j['source'])})",
        f"📌 {e(j['title'])}",
        f"💼 Experience: <b>{e(j['exp'])}</b>   (you: ~{MY_EXP_YEARS} yr)",
        f"{icon} {e(j['mode'])} · {e(j['location'])}" + (f" · 🗓 {j['posted']}" if j["posted"] else ""),
        f"✅ You have: {e(j['matched'] or '-')}",
    ]
    if j["gaps"]:
        lines.append(f"⚠️ Also asks (you lack): {e(j['gaps'])}")
    lines.append(f"🔗 <a href=\"{e(j['link'], quote=True)}\">Apply Here</a>")
    return "\n".join(lines)


def send_telegram(jobs):
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    blocks = [fmt(j) for j in jobs]
    if not token or not chat_id:
        print("\n=== MATCHED JOBS ===\n")
        print("\n\n".join(re.sub(r"<[^>]+>", "", b) for b in blocks))
        return
    a = sum(1 for j in jobs if j["tier"] == "A")
    header = (f"🚀 <b>Job Radar: {len(jobs)} new jobs ({a} tier-A) · "
              f"{len({j['company'].lower() for j in jobs})} companies · last {max(1, HOURS_OLD // 24)}d</b>\n\n")
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n\n"
    msgs.append(cur)
    for m in msgs:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": m, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=15)
            if r.status_code != 200:
                print(f"Telegram error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Telegram send error: {e}")
        time.sleep(1)


if __name__ == "__main__":
    print(f"Window {HOURS_OLD}h | your exp {MY_EXP_YEARS}y (accepting jobs asking <= {MAX_MIN_EXP}y) "
          f"| remote_only={REMOTE_ONLY}")
    print("Scanning company career pages...")
    fetch_career_pages()
    print(f"  career-page matches so far: {len(JOBS)}")
    print(f"Scanning LinkedIn/Indeed/Naukri ({len(SEARCH_QUERIES) * len(SEARCH_PLAN)} searches)...")
    fetch_portals()

    tier_rank = {"A": 0, "B": 1}
    mode_rank = {"Remote": 0, "Hybrid": 1, "Onsite": 2}
    JOBS.sort(key=lambda j: (tier_rank[j["tier"]], mode_rank[j["mode"]], -j["pct"]))
    print(f"Total new matches: {len(JOBS)} from {len({j['company'].lower() for j in JOBS})} companies")
    if FAILED_SLUGS:
        print("Career-page slugs not found: " + ", ".join(sorted(FAILED_SLUGS)))

    if JOBS:
        save_csv(JOBS)                         # everything goes to the CSV
        send_telegram(JOBS[:MAX_ALERTS])       # best ones go to Telegram
        for j in JOBS:
            STATE[j["_key"]] = NOW.isoformat()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
    else:
        print("No new matching jobs.")
