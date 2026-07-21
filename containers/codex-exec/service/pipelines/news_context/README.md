# news_context

`news_context` is a read-only deterministic pipeline step. It reads the current
symbol-news YAML cache and the persistent market-news SQLite database, chooses
the window since the most recent earlier completed daily-trading run (bounded to 72
hours), deduplicates both scopes, and writes run-local `news-context.json`.

It performs no network calls and never submits orders.
