import logging

import modal

app = modal.App("healthcare-reddit-mirror")

image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")

posts_dict = modal.Dict.from_name("healthcare-reddit-posts", create_if_missing=True)

RSS_URL = "https://www.reddit.com/r/healthcare.rss"
RSS_NEW_URL = "https://www.reddit.com/r/healthcare/new.rss"
USER_AGENT = "modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)"
AMPLITUDE_URL = "https://api2.amplitude.com/2/httpapi"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

log = logging.getLogger(__name__)


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


def _send_to_amplitude(posts: list[dict]) -> None:
    import os
    import time

    import httpx

    api_key = os.environ["AMPLITUDE_API_KEY"]
    now = time.time()
    events = [
        {
            "user_id": "reddit-mirror",
            "device_id": "reddit-mirror",
            "event_type": "reddit_post_ingested",
            "time": int(p["created_utc"] * 1000),
            "insert_id": f"reddit-{p['id']}",
            "event_properties": {
                "title": p["title"],
                "link": p["link"],
                "author": p["author"],
                "post_age_minutes": round((now - p["created_utc"]) / 60),
                "post_position": i + 1,
                "content_length": len(p.get("content", "")),
                "is_question": p["title"].rstrip().endswith("?"),
                "feed_source": p.get("feed_source", "hot"),
            },
        }
        for i, p in enumerate(posts)
    ]
    try:
        resp = httpx.post(
            AMPLITUDE_URL,
            json={"api_key": api_key, "events": events},
            timeout=10,
        )
        resp.raise_for_status()
        log.warning("Amplitude: sent %d events, status %d", len(events), resp.status_code)
    except httpx.HTTPError as e:
        log.error("Amplitude send failed: %s", e)


@app.function(
    image=image,
    schedule=modal.Cron("*/5 * * * *"),
    secrets=[modal.Secret.from_name("amplitude-secret")],
)
def poll_reddit():
    import time

    hot_posts = _fetch_reddit(RSS_URL)
    new_posts_raw = _fetch_reddit(RSS_NEW_URL)

    if hot_posts is None and new_posts_raw is None:
        return

    # Merge both feeds — hot takes precedence for duplicates
    all_posts: dict[str, dict] = {}
    hot_ids: set[str] = set()
    new_ids_raw: set[str] = set()

    if new_posts_raw:
        for p in new_posts_raw:
            all_posts[p["id"]] = p
            new_ids_raw.add(p["id"])
    if hot_posts:
        for p in hot_posts:
            all_posts[p["id"]] = p
            hot_ids.add(p["id"])

    # Tag feed_source per post
    for pid, p in all_posts.items():
        in_hot = pid in hot_ids
        in_new = pid in new_ids_raw
        p["feed_source"] = "both" if (in_hot and in_new) else ("hot" if in_hot else "new")

    current_ids = set(all_posts.keys())

    try:
        seen_ids = posts_dict["seen_ids"]
    except KeyError:
        seen_ids = set()

    genuinely_new_ids = current_ids - seen_ids
    genuinely_new = [all_posts[pid] for pid in genuinely_new_ids]

    if genuinely_new:
        _send_to_amplitude(genuinely_new)

    # Write front_page (hot sort) only if hot feed succeeded
    if hot_posts is not None:
        posts_dict["front_page"] = hot_posts
    seen_ids = seen_ids | current_ids
    if len(seen_ids) > 5000:
        seen_ids = current_ids
    posts_dict["seen_ids"] = seen_ids
    posts_dict["last_polled"] = time.time()

    log.warning(
        "Polled %d hot + %d new, %d genuinely new: %s",
        len(hot_posts or []),
        len(new_posts_raw or []),
        len(genuinely_new),
        [p["title"][:50] for p in genuinely_new],
    )


# --- Web UI ---

import fastapi

web_app = fastapi.FastAPI()


def _relative_time(unix_ts: float) -> str:
    import time

    delta = int(time.time() - unix_ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _render_html(posts: list[dict], last_polled: float | None = None) -> str:
    import html

    if not posts:
        rows = (
            '<tr><td colspan="3" style="text-align:center;color:#888;">'
            "The poller runs every 5 minutes — check back shortly."
            "</td></tr>"
        )
    else:
        rows = ""
        for p in posts:
            title = html.escape(p["title"])
            link = html.escape(p["link"])
            author = html.escape(p["author"])
            content = html.escape(p.get("content", ""))
            age = _relative_time(p["created_utc"])
            snippet = (
                f'<br><span class="snippet">{content}</span>' if content else ""
            )
            author_url = f"https://www.reddit.com/user/{author}"
            rows += (
                f'<tr><td><a href="{link}" target="_blank" rel="noopener">'
                f"{title}</a>{snippet}</td>"
                f'<td><a href="{author_url}" target="_blank" rel="noopener">{author}</a></td>'
                f'<td class="age">{age}</td></tr>\n'
            )

    footer = ""
    if last_polled:
        footer = f'<p class="updated">Last updated {_relative_time(last_polled)}</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="300">
    <title>r/healthcare — Mirror</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #222; background: #fafafa; }}
        h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
        .subtitle {{ color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }}
        table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px;
                 overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
        th, td {{ text-align: left; padding: 0.6rem 0.75rem; border-bottom: 1px solid #f0f0f0; }}
        th {{ font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em;
              background: #f8f8f8; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover {{ background: #f9f9ff; }}
        a {{ color: #1a0dab; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .snippet {{ color: #666; font-size: 0.8rem; display: block; margin-top: 0.25rem;
                    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 500px; }}
        .age {{ color: #888; font-size: 0.85rem; white-space: nowrap; }}
        .updated {{ text-align: center; color: #999; font-size: 0.8rem; margin-top: 1rem; }}
    </style>
</head>
<body>
    <h1>r/healthcare — Front Page</h1>
    <p class="subtitle">Live mirror · updates every 5 min</p>
    <table>
        <thead><tr><th>Title</th><th>Author</th><th>Posted</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    {footer}
</body>
</html>"""


@web_app.get("/")
def home():
    try:
        posts = posts_dict["front_page"]
    except KeyError:
        posts = []
    try:
        last_polled = posts_dict["last_polled"]
    except KeyError:
        last_polled = None
    return fastapi.responses.HTMLResponse(content=_render_html(posts, last_polled))


@web_app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.function(image=image)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def serve():
    return web_app
