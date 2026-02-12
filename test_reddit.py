import modal

app = modal.App("reddit-test")
image = modal.Image.debian_slim(python_version="3.12").pip_install("httpx")


@app.local_entrypoint()
def main():
    test_urls.remote()


@app.function(image=image)
def test_urls():
    import httpx

    ua = "modal:healthcare-reddit-mirror:v1.0 (by /u/health-mirror-bot)"

    # Test 1: .json endpoint (expected: 403 from Modal)
    print("=== Test 1: .json endpoint ===")
    try:
        r = httpx.get(
            "https://www.reddit.com/r/healthcare.json",
            headers={"User-Agent": ua},
            timeout=15,
            follow_redirects=True,
        )
        print(f".json => {r.status_code}")
        print(f"Content-Type: {r.headers.get('content-type')}")
        print(f"Body (first 200): {r.text[:200]}")
    except Exception as e:
        print(f".json => ERROR: {e}")
    print()

    # Test 2: RSS endpoint
    print("=== Test 2: RSS endpoint ===")
    try:
        r = httpx.get(
            "https://www.reddit.com/r/healthcare.rss",
            headers={"User-Agent": ua},
            timeout=15,
            follow_redirects=True,
        )
        print(f"RSS => {r.status_code}")
        print(f"Content-Type: {r.headers.get('content-type')}")
        print(f"Body length: {len(r.text)}")
    except Exception as e:
        print(f"RSS => ERROR: {e}")
        return
    print()

    if r.status_code != 200:
        print("RSS not 200, aborting parse")
        return

    # Parse the RSS feed
    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    print(f"Found {len(entries)} entries\n")

    # Check all fields for first 3 entries
    for i, entry in enumerate(entries[:3]):
        title = entry.find("atom:title", ns)
        link = entry.find("atom:link", ns)
        author = entry.find("atom:author/atom:name", ns)
        post_id = entry.find("atom:id", ns)
        updated = entry.find("atom:updated", ns)
        category = entry.find("atom:category", ns)
        content = entry.find("atom:content", ns)
        print(f"--- Entry {i+1} ---")
        print(f"Title:    {title.text if title is not None else 'N/A'}")
        print(f"Link:     {link.get('href') if link is not None else 'N/A'}")
        print(f"Author:   {author.text if author is not None else 'N/A'}")
        print(f"ID:       {post_id.text if post_id is not None else 'N/A'}")
        print(f"Updated:  {updated.text if updated is not None else 'N/A'}")
        print(f"Category: {category.get('term') if category is not None else 'N/A'}")
        print(f"Content:  {content.text[:100] if content is not None and content.text else 'N/A'}")
        print()

    # Show all tag names in first entry for completeness
    print("=== All tags in first entry ===")
    if entries:
        for elem in entries[0]:
            tag = elem.tag.replace("{http://www.w3.org/2005/Atom}", "atom:")
            attrs = dict(elem.attrib)
            text = (elem.text or "")[:80]
            print(f"  {tag}: text={text!r} attrs={attrs}")
            for child in elem:
                ctag = child.tag.replace("{http://www.w3.org/2005/Atom}", "atom:")
                print(f"    {ctag}: text={(child.text or '')[:80]!r}")
