"""Data access, kept out of the routes and services.

A repository is the one place that builds queries. Each module here owns the
reads for one slice of the schema — `users`, `fleet`, `commerce`, `ops` — and
exposes them as named functions (`users.by_email`, `fleet.active_bus`, …) so a
handler asks *what* it wants, never *how* to select it. That is the whole point:

- **Routes** stop carrying `select(...)` / `db.get(...)`. A route that reads
  "the caller's own active ticket" now says exactly that, and the join lives in
  one place that a query change or an index can be reasoned about from.
- **Services** call repositories for their lookups too, so the same query is not
  written twice and cannot drift between the request path and the worker path.

What a repository does **not** do: it never commits and never rolls back — the
route owns the transaction (see app/services/__init__.py). It holds no business
rules either; deciding what a row *means* belongs to the service or route that
called it. A repository fetches, and stages writes with `db.add`; the caller
flushes and commits. Passing the `AsyncSession` in (rather than each repository
holding one) keeps every call inside the caller's single unit of work.
"""
