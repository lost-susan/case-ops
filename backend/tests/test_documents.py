import hashlib
import os

from app.db import session_factory
from app.models.orm import Document, Event
from app.services.task_router import run_ingestion
from conftest import API, PDF, make_case, upload
from sqlalchemy import func, select


def test_upload_is_hashed_processed_and_linked_to_proof(authed, settings):
    cid = make_case(authed)
    r = upload(authed, cid, "ezpass.pdf")
    assert r.status_code == 202
    doc = r.json()
    assert doc["sha256"] == hashlib.sha256(PDF).hexdigest() and doc["size_bytes"] == len(PDF)
    assert authed.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()["status"] == "awaiting_extraction"

    events = authed.get(f"{API}/cases/{cid}/timeline").json()
    assert len(events) == 1 and events[0]["status"] == "pending"
    src = events[0]["source"]
    assert src["filename"] == "ezpass.pdf" and src["locator"] == "p. 1"
    proof = authed.get(f"{API}/cases/{cid}/proofs/{src['line_id']}").json()
    assert proof["highlight_index"] == 0 and proof["lines"][0]["text"] == "FILE: ezpass.pdf"
    assert any(doc["sha256"] in ln["text"] for ln in proof["lines"])
    assert authed.get(f"{API}/cases/{cid}/summary").json()["indexed_files"] == 1


def test_rejects_wrong_types_fake_content_empty_and_oversize(authed):
    cid = make_case(authed)
    assert upload(authed, cid, "evil.exe", b"MZ\x90\x00").status_code == 415
    assert upload(authed, cid, "noext", PDF).status_code == 415
    assert upload(authed, cid, "page.html", b"<html>", "text/html").status_code == 415
    assert upload(authed, cid, "fake.pdf", b"MZ\x90\x00 not a pdf").status_code == 415
    assert upload(authed, cid, "fake.png", PDF).status_code == 415
    assert upload(authed, cid, "chat.txt", b"hello\x00world").status_code == 415
    assert upload(authed, cid, "empty.pdf", b"").status_code == 422
    assert upload(authed, cid, "big.pdf", b"%PDF-" + b"0" * (1024 * 1024)).status_code == 413  # over the 1 MiB cap
    assert upload(authed, cid, "huge.pdf", b"%PDF-" + b"0" * (3 * 1024 * 1024)).status_code == 413  # stopped by middleware
    assert upload(authed, cid, "ok.txt", "[2026-03-14] Alex: Leaving now — ünïcode".encode()).status_code == 202
    assert upload(authed, cid, "a.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00data").status_code == 202
    assert upload(authed, cid, "a.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ").status_code == 202


def test_duplicate_upload_is_rejected_and_leaves_no_orphan_file(authed, settings):
    cid = make_case(authed)
    assert upload(authed, cid, "one.pdf").status_code == 202
    assert upload(authed, cid, "same-bytes.pdf").status_code == 409
    files = [f for f in os.listdir(settings.storage_dir)]
    assert len(files) == 1 and all(len(f) == 32 for f in files)


def test_hostile_filenames_never_reach_the_filesystem(authed, settings):
    cid = make_case(authed)
    r = upload(authed, cid, "../../../etc/passwd.pdf", PDF + b"1")
    assert r.status_code == 202 and r.json()["filename"] == "passwd.pdf"
    r = upload(authed, cid, 'we"ird\r\nname<script>.pdf', PDF + b"2")
    assert r.status_code == 202 and not any(c in r.json()["filename"] for c in '\r\n<>"')
    assert all(len(f) == 32 for f in os.listdir(settings.storage_dir))  # only random keys on disk


def test_download_is_attachment_nosniff_and_audited(authed):
    cid = make_case(authed)
    doc = upload(authed, cid, "evidence.pdf").json()
    r = authed.get(f"{API}/cases/{cid}/documents/{doc['id']}/download")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment") and r.headers["x-content-type-options"] == "nosniff"
    actions = [e["action"] for e in authed.get(f"{API}/cases/{cid}/audit").json()]
    assert "document.downloaded" in actions and "document.uploaded" in actions


def test_ingestion_runs_once(authed, settings):
    cid = make_case(authed)
    doc = upload(authed, cid).json()
    run_ingestion(__import__("uuid").UUID(doc["id"]), settings.storage_dir, "mock")  # a retry/duplicate task
    with session_factory()() as db:
        assert db.scalar(select(func.count(Event.id))) == 1
        assert db.scalar(select(Document.status)) == "awaiting_extraction"
