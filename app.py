import logging

import fastapi
import modal
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)

app = modal.App("reddit-mirror")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]")
    .add_local_dir("frontend/dist", remote_path="/frontend/dist")
)

posts_dict = modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)

RSS_URL = "https://www.reddit.com/r/healthcare.rss"
RSS_NEW_URL = "https://www.reddit.com/r/healthcare/new.rss"
USER_AGENT = "modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)"
AMPLITUDE_URL = "https://api2.amplitude.com/2/httpapi"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

log = logging.getLogger(__name__)

_TOPIC_KEYWORDS = {
    "insurance_billing": [
        "insurance", "billing", "payment", "cost", "claim", "coverage",
        "copay", "deductible", "premium", "afford", "charge", "price",
        "out of pocket", "medicaid", "medicare", "uninsured",
    ],
    "policy_regulation": [
        "rfk", "trump", "legislation", "regulation", "fda", "cdc",
        "cms", "aca", "obamacare", "policy", "reform", "mandate",
        "congress", "cdpap", "foreign aid", "executive order",
    ],
    "health_tech": [
        " ai ", "ehr", "emr", "scribe", "software", "automation",
        "telehealth", "telemedicine", "dashboard", "fax",
        "work queue", "charting", "epic",
    ],
    "career_workforce": [
        "career", "interview", "degree", "certification", "salary",
        "hiring", "sonography", "nursing", "residency", "burnout",
        "documentation burden", "workforce", "considering leaving",
    ],
    "patient_experience": [
        "diagnosis", "prescription", "medication", "adderall", "symptom",
        "treatment", "blood donation", "is it normal",
        "psych eval", "medical record", "medical tourism", "screening",
        "cancer",
    ],
}


def _categorize_topic(title: str, content: str) -> str:
    text = f" {title} {content} ".lower()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return topic
    return "other"


def _fetch_reddit(url: str = RSS_URL) -> list[dict] | None:
    import re
    import xml.etree.ElementTree as ET
    from datetime import datetime
    from html import unescape

    import httpx

    try:
        resp = httpx.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=15
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except (httpx.HTTPError, ET.ParseError) as e:
        log.error("Reddit fetch failed: %s", e)
        return None

    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        post_id = entry.findtext("atom:id", "", _ATOM_NS).removeprefix("t3_")
        link_el = entry.find("atom:link", _ATOM_NS)
        author = (
            entry.findtext("atom:author/atom:name", "", _ATOM_NS)
            .removeprefix("/u/")
        )
        published = entry.findtext("atom:published", "", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        content_html = content_el.text if content_el is not None and content_el.text else ""
        content_text = re.sub(r"<[^>]+>", "", unescape(content_html)).strip()
        content_text = re.sub(
            r"\s*submitted by\s+/u/\S+\s*", " ", content_text
        ).strip()
        content_text = re.sub(r"\s*\[link\]\s*", " ", content_text).strip()
        content_text = re.sub(r"\s*\[comments\]\s*", " ", content_text).strip()
        content_text = content_text[:200]
        posts.append(
            {
                "id": post_id,
                "title": entry.findtext("atom:title", "", _ATOM_NS),
                "link": link_el.get("href", "") if link_el is not None else "",
                "author": author,
                "created_utc": datetime.fromisoformat(published).timestamp()
                if published
                else 0.0,
                "content": content_text,
            }
        )
    return posts


def _send_to_amplitude(posts: list[dict], hot_rank: dict[str, int] | None = None) -> None:
    import os
    import time

    import httpx

    if hot_rank is None:
        hot_rank = {}
    api_key = os.environ["AMPLITUDE_API_KEY"]
    now = time.time()
    events = [
        {
            "user_id": f"reddit:{p['author']}",
            "device_id": "reddit-mirror",
            "event_type": "reddit_post_ingested",
            "time": int(p["created_utc"] * 1000),
            "insert_id": f"reddit-{p['id']}",
            "event_properties": {
                "title": p["title"],
                "link": p["link"],
                "author": p["author"],
                "post_age_minutes": round((now - p["created_utc"]) / 60),
                "post_position": hot_rank.get(p["id"], 0),
                "content_length": len(p.get("content", "")),
                "is_question": "?" in p["title"],
                "topic": _categorize_topic(p["title"], p.get("content", "")),
                "has_content": bool(p.get("content")),
            },
        }
        for p in posts
    ]
    try:
        resp = httpx.post(
            AMPLITUDE_URL,
            json={"api_key": api_key, "events": events},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Amplitude error %d: %s", resp.status_code, resp.text)
        else:
            log.info("Amplitude: sent %d events, status %d", len(events), resp.status_code)
    except httpx.HTTPError as e:
        log.error("Amplitude send failed: %s", e)


@app.function(
    image=image,
    schedule=modal.Cron("*/5 * * * *"),
    secrets=[modal.Secret.from_name("amplitude-secret")],
    retries=2,
)
def poll_reddit():
    import time

    hot_posts = _fetch_reddit(RSS_URL)
    new_posts_raw = _fetch_reddit(RSS_NEW_URL)

    if hot_posts is None and new_posts_raw is None:
        return

    # Merge both feeds — hot takes precedence for duplicates
    all_posts: dict[str, dict] = {}
    if new_posts_raw:
        for p in new_posts_raw:
            all_posts[p["id"]] = p
    if hot_posts:
        for p in hot_posts:
            all_posts[p["id"]] = p

    current_ids = set(all_posts.keys())

    try:
        seen_ids = posts_dict["seen_ids"]
    except KeyError:
        seen_ids = set()

    genuinely_new_ids = current_ids - seen_ids

    # Build position lookup from hot feed so Amplitude gets real ranks
    hot_rank = {p["id"]: i + 1 for i, p in enumerate(hot_posts)} if hot_posts else {}
    genuinely_new = [all_posts[pid] for pid in genuinely_new_ids]

    if genuinely_new:
        _send_to_amplitude(genuinely_new, hot_rank)

    # Write front_page (hot sort) only if hot feed succeeded
    if hot_posts is not None:
        posts_dict["front_page"] = hot_posts
    seen_ids = seen_ids | current_ids
    if len(seen_ids) > 5000:
        seen_ids = current_ids
    posts_dict["seen_ids"] = seen_ids
    posts_dict["last_polled"] = time.time()

    log.info(
        "Polled %d hot + %d new, %d genuinely new: %s",
        len(hot_posts or []),
        len(new_posts_raw or []),
        len(genuinely_new),
        [p["title"][:50] for p in genuinely_new],
    )


# --- Web API ---

web_app = fastapi.FastAPI()


@web_app.get("/api/posts")
def api_posts():
    try:
        posts = posts_dict["front_page"]
    except KeyError:
        posts = []
    try:
        last_polled = posts_dict["last_polled"]
    except KeyError:
        last_polled = None
    return {"posts": posts, "last_polled": last_polled}


@web_app.get("/healthz")
def healthz():
    return {"status": "ok"}


@web_app.get("/")
def home():
    return FileResponse("/frontend/dist/index.html")


web_app.mount(
    "/assets",
    StaticFiles(directory="/frontend/dist/assets", check_dir=False),
    name="static",
)


@app.function(image=image)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def serve():
    return web_app
