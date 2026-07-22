"""Business logic, kept out of the route handlers.

A route's job is HTTP: parse the request, call one service function, shape the
response. Anything that would still be true if the API were a CLI belongs here.

Why bother, at this size? Because trip lifecycle is about to be called from
three places — the helper endpoints, the admin panel, and the worker that
auto-closes abandoned trips — and logic that lives in a route handler can only
be reached over HTTP.
"""
