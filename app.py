import logging

import modal

app = modal.App("healthcare-reddit-mirror")

image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")

posts_dict = modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)

RSS_URL = "https://www.reddit.com/r/healthcare.rss"
USER_AGENT = "modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

log = logging.getLogger(__name__)


def _fetch_reddit() -> list[dict] | None:
    import xml.etree.ElementTree as ET
    from datetime import datetime

    import httpx

    try:
        resp = httpx.get(
            RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=15
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
        posts.append(
            {
                "id": post_id,
                "title": entry.findtext("atom:title", "", _ATOM_NS),
                "link": link_el.get("href", "") if link_el is not None else "",
                "author": author,
                "created_utc": datetime.fromisoformat(published).timestamp()
                if published
                else 0.0,
            }
        )
    return posts


@app.function(image=image, schedule=modal.Cron("*/5 * * * *"))
def poll_reddit():
    posts = _fetch_reddit()
    if posts is None:
        return

    current_ids = {p["id"] for p in posts}

    try:
        seen_ids = posts_dict["seen_ids"]
    except KeyError:
        seen_ids = set()

    new_ids = current_ids - seen_ids
    new_posts = [p for p in posts if p["id"] in new_ids]

    # Write front_page first (UI freshness), then seen_ids
    posts_dict["front_page"] = posts
    seen_ids = seen_ids | current_ids
    if len(seen_ids) > 5000:
        seen_ids = current_ids
    posts_dict["seen_ids"] = seen_ids

    log.warning(
        "Polled %d posts, %d new: %s",
        len(posts),
        len(new_posts),
        [p["title"][:50] for p in new_posts],
    )


# --- Web UI (Phase 1 skeleton) ---

import fastapi

web_app = fastapi.FastAPI()


@web_app.get("/")
def home():
    return fastapi.responses.HTMLResponse("Hello")


@app.function(image=image)
@modal.asgi_app()
def serve():
    return web_app
