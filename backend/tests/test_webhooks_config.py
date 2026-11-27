import hashlib
import hmac
import json
import time
import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from conftest import API, make_case, upload


def sign(secret, body: bytes, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    return {"X-Timestamp": ts, "X-Signature": "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()}


def test_webhook_signature_window_and_replay(authed, client, settings):
    cid = make_case(authed)
    # an "awaiting_extraction" document can be finalised by a signed callback
    doc = upload(authed, cid).json()
    body = json.dumps({"document_id": doc["id"], "status": "parsed", "confidence": 97.5}).encode()
    url = f"{API}/webhooks/ingestion"
    hdr = {"Content-Type": "application/json"}
    assert client.post(url, content=body, headers=hdr).status_code == 401  # unsigned
    assert client.post(url, content=body, headers={**hdr, **sign("x" * 48, body)}).status_code == 401  # wrong secret
    assert client.post(url, content=body, headers={**hdr, **sign(settings.webhook_secret, body, time.time() - 3600)}).status_code == 401  # stale
    assert client.post(url, content=body + b" ", headers={**hdr, **sign(settings.webhook_secret, body)}).status_code == 401  # tampered
    ok = client.post(url, content=body, headers={**hdr, **sign(settings.webhook_secret, body)})
    assert ok.status_code == 204
    d = authed.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()
    assert d["status"] == "parsed" and d["ocr_confidence"] == 97.5
    assert client.post(url, content=body, headers={**hdr, **sign(settings.webhook_secret, body)}).status_code == 409  # finalised


def test_webhook_rejects_invalid_payloads_after_authentication(client, settings):
    for payload in ({"document_id": "nope", "status": "parsed"}, {"document_id": str(uuid.uuid4()), "status": "pwned"},
                    {"document_id": str(uuid.uuid4()), "status": "parsed", "confidence": 101}, {"extra": 1}):
        body = json.dumps(payload).encode()
        r = client.post(f"{API}/webhooks/ingestion", content=body, headers={"Content-Type": "application/json", **sign(settings.webhook_secret, body)})
        assert r.status_code == 422


def test_settings_refuse_unsafe_configuration():
    base = dict(database_url="sqlite://", secret_key="s" * 40, webhook_secret="w" * 40)
    with pytest.raises(ValidationError):
        Settings(**{**base, "secret_key": "short"})
    with pytest.raises(ValidationError):
        Settings(**base, cookie_samesite="none", cookie_secure=False)
    with pytest.raises(ValidationError):
        Settings(**base, env="production", cookie_secure=False)
    with pytest.raises(ValidationError):
        Settings(**base, cors_origins=["*"])
    assert Settings(**base).cookie_secure is True  # secure by default
