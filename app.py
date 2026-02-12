import logging

import modal

app = modal.App("healthcare-reddit-mirror")

image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")

posts_dict = modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)

REDDIT_URL = "https://www.reddit.com/r/healthcare.json"
USER_AGENT = "modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)"

log = logging.getLogger(__name__)


def _fetch_reddit() -> list[dict] | None:
    import httpx

    try:
        resp = httpx.get(
            REDDIT_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error("Reddit fetch failed: %s", e)
        return None

    posts = []
    for child in resp.json()["data"]["children"]:
        d = child["data"]
        posts.append(
            {
                "id": d["id"],
                "title": d["title"],
                "author": d["author"],
                "permalink": d["permalink"],
                "link": "https://www.reddit.com" + d["permalink"],
                "score": d["score"],
                "num_comments": d["num_comments"],
                "created_utc": d["created_utc"],
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
