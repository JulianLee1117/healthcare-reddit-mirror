const ALLOWED_PATHS = new Set([
  "/r/healthcare.json",
  "/r/healthcare/new.json",
]);

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    if (!ALLOWED_PATHS.has(pathname)) {
      return new Response("Not found", { status: 404 });
    }
    const resp = await fetch(`https://www.reddit.com${pathname}`, {
      headers: {
        "User-Agent": "healthcare-mirror/1.0 (+contact: cloudflare-relay)",
      },
    });
    return new Response(resp.body, {
      status: resp.status,
      headers: { "Content-Type": resp.headers.get("Content-Type") || "text/plain" },
    });
  },
};
