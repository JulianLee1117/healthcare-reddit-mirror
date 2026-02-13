import { useState, useEffect } from "react";

function relativeTime(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function Post({ post, rank }) {
  const isQuestion = post.title.includes("?");
  return (
    <article className="post">
      <span className="post-rank">{rank}</span>
      <div className="post-body">
        <div className="post-title-row">
          <a
            href={post.link}
            target="_blank"
            rel="noopener"
            className="post-title"
          >
            {post.title}
          </a>
          {isQuestion && <span className="badge badge-question">Q</span>}
        </div>
        {post.content && <p className="post-snippet">{post.content}</p>}
        <div className="post-meta">
          <a
            href={`https://www.reddit.com/user/${post.author}`}
            target="_blank"
            rel="noopener"
            className="post-author"
          >
            {post.author}
          </a>
          <span className="meta-sep">&middot;</span>
          <time>{relativeTime(post.created_utc)}</time>
        </div>
      </div>
    </article>
  );
}

export default function App() {
  const [posts, setPosts] = useState([]);
  const [lastPolled, setLastPolled] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = () =>
      fetch("/api/posts")
        .then((r) => r.json())
        .then((d) => {
          setPosts(d.posts || []);
          setLastPolled(d.last_polled);
        })
        .catch(() => {})
        .finally(() => setLoading(false));
    load();
    const id = setInterval(load, 300_000);
    return () => clearInterval(id);
  }, []);

  const questionCount = posts.filter((p) => p.title.includes("?")).length;

  return (
    <div className="app">
      <header className="header">
        <div className="header-top">
          <h1>r/healthcare</h1>
          {lastPolled && (
            <span className="last-polled">
              Polled {relativeTime(lastPolled)}
            </span>
          )}
        </div>
        {!loading && posts.length > 0 && (
          <div className="stats-bar">
            <div className="stat">
              <span className="stat-value">{posts.length}</span>
              <span className="stat-label">posts</span>
            </div>
            <div className="stat">
              <span className="stat-value">{questionCount}</span>
              <span className="stat-label">questions</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                {posts.length - questionCount}
              </span>
              <span className="stat-label">links / discussion</span>
            </div>
          </div>
        )}
      </header>

      <main>
        {loading ? (
          <div className="status">
            <div className="spinner" />
            <p>Loading posts...</p>
          </div>
        ) : posts.length === 0 ? (
          <div className="status">
            <p>The poller runs every 5 minutes — check back shortly.</p>
          </div>
        ) : (
          <div className="posts">
            {posts.map((p, i) => (
              <Post key={p.id} post={p} rank={i + 1} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
