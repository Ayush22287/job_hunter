import os
import html
import requests
from datetime import datetime, timedelta, timezone

# Optional import for portal-wide scraping (LinkedIn, Indeed, Naukri)
try:
    from jobspy import scrape_jobs
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False

# ==========================================
# 1. TARGET COMPANIES (105+ Direct Career Page APIs)
# ==========================================
GREENHOUSE_COMPANIES = [
    # Observability, Monitoring & SRE Giants
    "grafanalabs", "datadog", "newrelic", "elastic", "honeycomb", "chronosphere",
    "logicmonitor", "sumologic", "pagerduty", "cribl", "kentik", "catchpoint",
    # Cloud Infrastructure, DevOps & Containers
    "gitlab", "cloudflare", "hashicorp", "digitalocean", "fastly", "netlify",
    "vercel", "circleci", "jfrog", "pulumi", "kong", "chainguard", "spectrocloud",
    "aviatrix", "alkira", "armory", "harness", "buildkite", "octopusdeploy",
    # Data, Storage & Cloud Platforms
    "databricks", "rubrik", "cohesity", "purestorage", "netapp", "couchbase",
    "cockroachlabs", "SingleStore", "neo4j", "redis", "timescale", "planetscale",
    "starburst", "fivetran", "dbt-labs", "airbyte", "astronomer", "confluent",
    # Cybersecurity & Cloud Security (High Cloud Ops / SRE hiring in India)
    "crowdstrike", "zscaler", "okta", "sentinelone", "snyk", "wiz", "orca-security",
    "abnormal-security", "lacework", "illumio", "tanium", "beyondtrust", "varonis",
    # Global SaaS & Remote-First Tech with India Hubs
    "stripe", "twilio", "zendesk", "hubspot", "postman", "hasura", "browserstack",
    "chargebee", "razorpay", "clevertap", "moengage", "freshworks", "druva",
    "innovaccer", "gupshup", "yugabyte", "appsmith", "remote", "deel", "canonical"
]

LEVER_COMPANIES = [
    "rackspace", "veeam", "zimperium", "resilinc", "actian", "kasten",
    "sysdig", "spotinst", "anchore", "solo-io", "buoyant", "linode",
    "scaleway", "percona", "scylladb", "questdb", "crate", "dremio",
    "hevodata", "yellow-ai", "plum", "sprinto", "leena-ai", "lambdatest"
]

ASHBY_COMPANIES = [
    "vultr", "clickhouse", "supabase", "render", "railway", "fly-io",
    "tailscale", "teleport", "dagger", "earthly", "infisical", "posthog",
    "langfuse", "checkly", "betterstack", "incident-io", "rootly", "firehydrant"
]

# ==========================================
# 2. AYUSH'S CUSTOM FILTERING RULES
# ==========================================
REQUIRED_ROLE_KEYWORDS = [
    "azure", "cloud", "sre", "site reliability", "observability",
    "monitoring", "devops", "production support", "infrastructure",
    "dynatrace", "noc", "platform", "operations", "support engineer"
]

REQUIRED_SKILL_KEYWORDS = [
    "azure", "dynatrace", "monitor", "kql", "log analytics", "application insights",
    "incident", "observability", "linux", "terraform", "kubernetes", "aks",
    "docker", "prometheus", "grafana", "alert", "troubleshooting", "sla"
]

EXCLUDE_TITLE_KEYWORDS = [
    "senior", "sr.", "sr ", "staff", "principal", "lead", "manager",
    "director", "architect", "head", "vp", "vice president", "distinguished", "iii"
]

LOCATION_KEYWORDS = ["india", "remote", "anywhere", "apac", "global", "work from home"]

# Fetch jobs created/updated in the last 48 hours
CUTOFF_TIME = datetime.now(timezone.utc) - timedelta(hours=48)
SEEN_URLS = set()


def matches_ayush_profile(title: str, location: str, description: str) -> bool:
    t_low = title.lower()
    l_low = location.lower()
    d_low = description.lower()

    # 1. Exclude Senior / Lead / Management titles
    if any(bad in t_low for bad in EXCLUDE_TITLE_KEYWORDS):
        return False

    # 2. Check Role Title Relevance
    if not any(role in t_low for role in REQUIRED_ROLE_KEYWORDS):
        return False

    # 3. Check Location Eligibility (India or Remote)
    if not any(loc in l_low for loc in LOCATION_KEYWORDS):
        return False

    # 4. Require at least 2 matching skills in the description (or title)
    combined_text = f"{t_low} {d_low}"
    skill_hits = sum(1 for skill in REQUIRED_SKILL_KEYWORDS if skill in combined_text)
    return skill_hits >= 2


def add_match(matches_list: list, company: str, title: str, location: str, link: str, source: str):
    if not link or link in SEEN_URLS:
        return
    SEEN_URLS.add(link)
    matches_list.append(
        f"🏢 *{company.upper()}* ({source})\n"
        f"📌 *{title}*\n"
        f"🌍 {location}\n"
        f"🔗 [Apply Here]({link})"
    )


# ==========================================
# 3. DIRECT CAREER PAGE FETCHERS (ATS APIs)
# ==========================================
def fetch_greenhouse_jobs() -> list:
    matches = []
    for company in GREENHOUSE_COMPANIES:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            for job in res.json().get("jobs", []):
                updated_str = job.get("updated_at")
                if not updated_str:
                    continue
                updated_dt = datetime.fromisoformat(updated_str)
                if updated_dt < CUTOFF_TIME:
                    continue

                title = job.get("title", "")
                location = job.get("location", {}).get("name", "")
                desc = html.unescape(job.get("content", ""))
                link = job.get("absolute_url", "")

                if matches_ayush_profile(title, location, desc):
                    add_match(matches, company, title, location, link, "Career Page")
        except Exception as e:
            print(f"[Greenhouse Error] {company}: {e}")
    return matches


def fetch_lever_jobs() -> list:
    matches = []
    for company in LEVER_COMPANIES:
        url = f"https://api.lever.co/v0/postings/{company}?mode=json"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            for job in res.json():
                created_ms = job.get("createdAt", 0)
                created_dt = datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc)
                if created_dt < CUTOFF_TIME:
                    continue

                title = job.get("text", "")
                categories = job.get("categories", {})
                location = f"{categories.get('location', '')} ({job.get('workplaceType', '')})"
                desc = job.get("descriptionPlain", "")
                link = job.get("hostedUrl", "")

                if matches_ayush_profile(title, location, desc):
                    add_match(matches, company, title, location, link, "Career Page")
        except Exception as e:
            print(f"[Lever Error] {company}: {e}")
    return matches


def fetch_ashby_jobs() -> list:
    matches = []
    for company in ASHBY_COMPANIES:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            for job in res.json().get("jobs", []):
                pub_str = job.get("publishedAt")
                if not pub_str:
                    continue
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                if pub_dt < CUTOFF_TIME:
                    continue

                title = job.get("title", "")
                is_rem = "Remote" if job.get("isRemote") else "Onsite/Hybrid"
                location = f"{job.get('location', '')} ({is_rem})"
                desc = job.get("descriptionPlain", "")
                link = job.get("jobUrl", "")

                if matches_ayush_profile(title, location, desc):
                    add_match(matches, company, title, location, link, "Career Page")
        except Exception as e:
            print(f"[Ashby Error] {company}: {e}")
    return matches


# ==========================================
# 4. ALL-INDIA PORTAL FETCHER (JobSpy)
# ==========================================
def fetch_portal_wide_jobs() -> list:
    if not JOBSPY_AVAILABLE:
        print("python-jobspy not installed, skipping portal-wide search.")
        return []

    matches = []
    search_queries = [
        "Azure Dynatrace",
        "Azure Cloud Operations Engineer",
        "Azure Monitoring Observability Engineer",
        "Junior Azure Cloud Engineer",
        "SRE Support Azure"
    ]

    for query in search_queries:
        try:
            df = scrape_jobs(
                site_name=["indeed", "linkedin", "naukri"],
                search_term=query,
                location="India",
                is_remote=True,
                hours_old=48,
                results_wanted=15,
                country_indeed="india"
            )
            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                title = str(row.get("title") or "")
                company = str(row.get("company") or "Company")
                location = str(row.get("location") or "Remote, India")
                desc = str(row.get("description") or "")
                site = str(row.get("site") or "Portal").capitalize()

                # Prefer direct career-page URL when available
                direct_url = row.get("job_url_direct")
                portal_url = row.get("job_url")
                link = str(direct_url if direct_url and str(direct_url) != "nan" else portal_url)

                # Filter out senior roles
                if any(bad in title.lower() for bad in EXCLUDE_TITLE_KEYWORDS):
                    continue

                # Ensure role title matches target domain
                if not any(role in title.lower() for role in REQUIRED_ROLE_KEYWORDS):
                    continue

                add_match(matches, company, title, location, link, f"via {site}")
        except Exception as e:
            print(f"[JobSpy Error] '{query}': {e}")

    return matches


# ==========================================
# 5. TELEGRAM NOTIFICATION SENDER
# ==========================================
def send_telegram_alert(messages: list):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("\n=== MATCHED JOBS ===")
        print("\n\n".join(messages))
        return

    header = f"🚀 *Ayush's Daily Job Radar: {len(messages)} Fresh Matches (Last 48h)*\n\n"
    for i in range(0, len(messages), 5):
        chunk_body = "\n\n".join(messages[i:i + 5])
        text = header + chunk_body if i == 0 else chunk_body
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                },
                timeout=10
            )
        except Exception as e:
            print(f"Telegram send error: {e}")


if __name__ == "__main__":
    print("Scanning 105+ Career Pages (Greenhouse, Lever, Ashby)...")
    ats_matches = fetch_greenhouse_jobs() + fetch_lever_jobs() + fetch_ashby_jobs()

    print("Scanning India-wide Remote Jobs (LinkedIn, Indeed, Naukri)...")
    portal_matches = fetch_portal_wide_jobs()

    all_matches = ats_matches + portal_matches
    print(f"Total unique matches found: {len(all_matches)}")

    if all_matches:
        send_telegram_alert(all_matches)
    else:
        print("No new matching
