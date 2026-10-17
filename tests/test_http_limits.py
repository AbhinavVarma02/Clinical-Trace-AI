import asyncio
import json

from api.middleware import MAX_BODY_BYTES, RequestContextMiddleware, TokenBucketLimiter


def test_chunked_or_understated_body_is_rejected_before_app():
    async def probe(headers):
        messages = iter([
            {"type": "http.request", "body": b"x" * 20000, "more_body": True},
            {"type": "http.request", "body": b"y" * 20000, "more_body": False},
        ])
        sent, logs = [], []

        async def never_called(scope, receive, send):
            raise AssertionError("oversized body reached the route")

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        middleware = RequestContextMiddleware(never_called, lambda *args: logs.append(args))
        await middleware({"type": "http", "method": "POST", "path": "/api/v2/agent/runs", "headers": headers,
                          "client": ("body-test", 100)}, receive, send)
        assert sent[0]["status"] == 413
        assert b"x-request-id" in dict(sent[0]["headers"])
        assert json.loads(sent[1]["body"])["error"]["code"] == "payload_too_large"
        assert logs[0][3] == 413 and logs[0][2] == "unmatched"

    assert MAX_BODY_BYTES < 40000
    asyncio.run(probe([]))
    asyncio.run(probe([(b"content-length", b"1")]))


def test_spoofed_forwarded_header_cannot_reset_rate_limit(api_client):
    from api.middleware import RATE_LIMITER

    old = RATE_LIMITER.limits.copy()
    RATE_LIMITER._buckets.clear()
    RATE_LIMITER.limits["default"] = 2
    try:
        responses = [api_client.get("/api/v2/patients", headers={"X-Demo-Persona": "clinician.demo",
                                                               "X-Forwarded-For": f"203.0.113.{i}"}) for i in range(4)]
        assert [r.status_code for r in responses] == [200, 200, 429, 429]
        assert responses[-1].headers["x-request-id"] == responses[-1].json()["error"]["request_id"]
    finally:
        RATE_LIMITER.limits.update(old)
        RATE_LIMITER._buckets.clear()


def test_limiter_memory_is_bounded():
    limiter = TokenBucketLimiter(10, 10, max_buckets=2)
    assert limiter.allow("first", "default")[0]
    assert limiter.allow("second", "default")[0]
    assert not limiter.allow("third", "default")[0]
    assert limiter.describe()["tracked_clients"] == 2
