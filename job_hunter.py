import os
import html
import requests
from datetime import datetime, timedelta, timezone

# 1. Target Companies (Add any company using Greenhouse or Lever)
GREENHOUSE_COMPANIES = ["gitlab", "grafanalabs", "elastic", "cloudflare", "databricks", "rubrik", "hashicorp"]
LEVER_COMPANIES = ["rackspace", "veeam"]

# 2. Ayush's Custom Filtering Rules
REQUIRED_ROLE_KEYWORDS = [
    "azure", "cloud", "sre", "site reliability", "observability", 
    "monitoring", "devops", "production support", "infrastructure", "dynatrace"
]
REQUIRED_SKILL_KEYWORDS = [
    "azure", "dynatrace", "monitor", "kql", "log analytics", 
    "incident", "observability", "linux", "terraform", "kubernetes"
]
EXCLUDE_TITLE_KEYWORDS = [
    "senior", "sr.", "staff", "principal", "lead", "manager", 
    "director", "architect", "head", "vp", "intern"
]
LOCATION_KEYWORDS = ["india", "remote", "anywhere", "apac", "global"]

# Only fetch jobs posted/updated in the last 48 hours
CUTOFF_TIME = datetime.now(timezone.utc) - timedelta(hours=48)

def matches_ayush_profile(title: str, location: str, description: str) -> bool:
    t_low = title.lower()
    l_low = location.lower()
    d_low = description.lower()

    # Rule A: Exclude senior/management titles
    if any(bad in t_low for bad in EXCLUDE_TITLE_KEYWORDS):
        return False

    # Rule B: Title must match target domain
    if not any(role in t_low for role in REQUIRED_ROLE_KEYWORDS):
        return False

    # Rule C: Location must be India-eligible or Remote
    if not any(loc in l_low for loc in LOCATION_KEYWORDS):
        return False

    # Rule D: Description must mention at least 2 of your core skills
    skill_hits = sum(1 for skill in REQUIRED_SKILL_KEYWORDS if skill in d_low)
    return skill_hits >= 2

def fetch_greenhouse_jobs():
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
                    matches.append(f"🏢 *{company.upper()}*\n📌 *{title}*\n🌍 {location}\n🔗 [Apply on Career Page]({link})")
        except Exception as e:
            print(f"Error fetching {company}: {e}")
    return matches

def fetch_lever_jobs():
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
                location = f"{categories.get('location', '')} ({ job.get('workplaceType', '') })"
                desc = job.get("descriptionPlain", "")
                link = job.get("hostedUrl", "")

                if matches_ayush_profile(title, location, desc):
                    matches.append(f"🏢 *{company.upper()}*\n📌 *{title}*\n🌍 {location}\n🔗 [Apply on Career Page]({link})")
        except Exception as e:
            print(f"Error fetching {company}: {e}")
    return matches

def send_telegram_alert(messages):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("\n--- MATCHED JOBS ---\n" + "\n\n".join(messages))
        return

    header = f"🚀 *Found {len(messages)} Fresh Career-Page Jobs Matching Your Profile:*\n\n"
    # Send in chunks to avoid Telegram message length limits
    for i in range(0, len(messages), 5):
        chunk = header + "\n\n".join(messages[i:i+5]) if i == 0 else "\n\n".join(messages[i:i+5])
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown", "disable_web_page_preview": True}
        )

if __name__ == "__main__":
    all_matches = fetch_greenhouse_jobs() + fetch_lever_jobs()
    if all_matches:
        send_telegram_alert(all_matches)
    else:
        print("No new matching jobs in the last 48 hours.")
