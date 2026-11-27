"""Pure-ASGI middleware: request body limits and security headers."""
import re

_UPLOAD_PATH = re.compile(r"^/api/v1/cases/[0-9a-fA-F-]{36}/documents/?$")
_MULTIPART_OVERHEAD = 1024 * 1024


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects oversized bodies before they are buffered: by Content-Length, and by counting streamed bytes."""

    def __init__(self, app, *, json_limit: int, upload_limit: int):
        self.app, self.json_limit, self.upload_limit = app, json_limit, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        is_upload = scope["method"] == "POST" and bool(_UPLOAD_PATH.match(scope["path"]))
        limit = self.upload_limit + _MULTIPART_OVERHEAD if is_upload else self.json_limit
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    return await self._reject(send)
            except ValueError:
                return await self._reject(send, 400)
        total, started = 0, False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send, status: int = 413):
        body = b'{"detail":"Request body too large"}' if status == 413 else b'{"detail":"Bad request"}'
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app, *, production: bool):
        self.app = app
        self.extra = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cache-control", b"no-store"),
        ]
        if production:  # the dev-only /docs UI needs inline scripts, so the strict CSP is production-only
            self.extra += [
                (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
            ]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in self.extra if h[0] not in present]
            await send(message)

        await self.app(scope, receive, send_with_headers)
