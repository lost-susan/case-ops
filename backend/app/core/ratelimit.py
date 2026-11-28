"""Per-client request rate limiting (fixed window, in memory). Pure ASGI.

Protects against floods and credential stuffing from one address. State is per process: with several workers or
replicas the effective limit is multiplied, so keep a proxy-level limit (nginx `limit_req`) as the primary defence.
Behind a proxy set CASEOPS_TRUST_PROXY_HEADERS=true to key on the right-most X-Forwarded-For entry (the one *your*
proxy appended); never enable it when the API is directly reachable, because clients could then spoof their address."""
import json
import time

MAX_KEYS = 50_000


class RateLimitMiddleware:
    def __init__(self, app, *, rules: list[tuple[str, int]], window: int = 60, trust_proxy: bool = False):
        # rules: (path prefix, max requests per window), first match wins; put the strictest prefixes first
        self.app, self.rules, self.window, self.trust_proxy = app, rules, window, trust_proxy
        self.hits: dict[tuple[str, str], tuple[int, int]] = {}  # (client, rule prefix) -> (window id, count)

    def _client(self, scope) -> str:
        if self.trust_proxy:
            for name, value in scope["headers"]:
                if name == b"x-forwarded-for":
                    return value.decode("latin-1").split(",")[-1].strip()[:64] or "unknown"
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        path = scope["path"]
        rule = next(((p, n) for p, n in self.rules if path.startswith(p)), None)
        if rule is None:
            return await self.app(scope, receive, send)
        now = time.monotonic()
        win = int(now // self.window)
        key = (self._client(scope), rule[0])
        w, count = self.hits.get(key, (win, 0))
        count = count + 1 if w == win else 1
        if len(self.hits) >= MAX_KEYS:
            self.hits = {k: v for k, v in self.hits.items() if v[0] == win}
        self.hits[key] = (win, count)
        if count > rule[1]:
            retry = max(1, int((win + 1) * self.window - now))
            body = json.dumps({"detail": "Too many requests. Slow down."}).encode()
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry).encode())]})
            return await send({"type": "http.response.body", "body": body})
        await self.app(scope, receive, send)
