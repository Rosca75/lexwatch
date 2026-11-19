# Decisions

Every choice made without the product owner, in the order it was made.
Format: date — decision. *Reason.* Alternatives considered.

## Process and repository

- **2026-11-19 — Integration branch.** Work is integrated on `claude/vibrant-brahmagupta-vajiup` (the branch this
  build session is bound to) and `main` is fast-forwarded to it once it is green. Feature streams use
  `feat/<stream>` branches in separate git worktrees and are merged back locally.
  *Reason:* the session environment designates that branch for development; the build prompt asks for
  everything to end up on `main`. *Alternatives:* PR per stream (rejected: the prompt says no PR review is wanted).
- **2026-11-19 — Docker daemon started inside the build container.** Docker was installed but not running; it
  was started with `dockerd` so that `docker compose up` could be verified end to end.
- **2026-11-19 — Tests run against a real PostgreSQL 16.** Full-text search (`french` configuration), generated
  `tsvector` columns and `JSONB` are PostgreSQL features; SQLite would hide bugs. Tests read `TEST_DATABASE_URL`
  (default `postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_test`).
  *Alternative:* SQLite in-memory (rejected).
