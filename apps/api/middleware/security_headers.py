"""Security headers on every API response.

The web app sets its own (apps/web/next.config.js); this covers the API, which
on Vercel is a separate service and in Docker sits behind Traefik.

Written as plain ASGI rather than `@app.middleware("http")`: that one wraps the
response in a way that can hold back streamed bodies, and the live-update
endpoint (routers/events.py) is a long-lived text/event-stream.
"""

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    # Use the declared Content-Type. Comment attachments are user uploads served
    # with the type they were stored with; sniffing could run one as HTML.
    (b"x-content-type-options", b"nosniff"),
    # No API response is meant to be shown in a frame.
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    # Keep API responses (JSON, HLS playlists, comment attachments) out of
    # search results. The web app sends the same header and a robots.txt.
    (b"x-robots-tag", b"noindex, nofollow"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                # A route that sets one of these on purpose keeps its value.
                headers.extend((name, value) for name, value in SECURITY_HEADERS if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
