"""Business logic, kept out of the route handlers.

A route's job is HTTP: parse the request, call service functions, shape the
response, and own the transaction. Anything that would still be true if the API
were a CLI belongs here.

Why bother, at this size? Because trip lifecycle is called from more than one
place — the helper endpoints, the admin panel, and the worker that auto-closes
abandoned trips — and logic that lives in a route handler can only be reached
over HTTP.

The transaction contract
------------------------
One rule, so a service can never surprise its caller: **service functions flush,
they do not commit or roll back.** The caller — the route, or a worker — owns the
transaction and commits exactly once. `flush()` is used where a function needs
the database to assign a primary key or enforce a constraint mid-operation (a
unique-index race, say); it sends the SQL without ending the transaction.

This is what lets a caller compose several service calls into one unit of work
and commit them together — or, like the payment reconciler and the redemption
batch, drive the boundary itself, one commit per item. A service that committed
on its own would silently finalize a caller's other pending writes; none here do.

**Side effects that must follow the commit** — warming a Redis cache, publishing
to the fleet or alerts channel — are separate functions the caller runs *after*
its `commit()` (`trip.cache_active_trip`, `ops.publish_seats`, …). So nothing is
cached or announced before the row it describes is durable, and a rolled-back
write leaves no ghost behind it.
"""
