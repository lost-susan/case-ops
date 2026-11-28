import io
import shutil
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai_engine.pipelines import dates
from ai_engine.pipelines.extraction import LocalProvider, parse_chat_line
from conftest import API, make_case, upload

NY = ZoneInfo("America/New_York")


@pytest.fixture
def settings(settings):  # run this module with the real local provider instead of the mock
    return settings.model_copy(update={"ingestion_provider": "local"})


def timeline(c, cid):
    return c.get(f"{API}/cases/{cid}/timeline").json()


def doc_status(c, cid, doc):
    return c.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()["status"]


# ---------- dates ----------
def test_date_finder_is_explicit_and_never_guesses():
    f = lambda s, order="mdy": dates.find_first(s, NY, order)
    assert f("Arraigned 2026-03-14 at 10:05 PM") == datetime(2026, 3, 14, 22, 5, tzinfo=NY)
    assert f("on March 14th, 2026") == datetime(2026, 3, 14, 12, 0, tzinfo=NY)  # no time -> noon, calendar day safe
    assert f("14 March 2026, 09:30") == datetime(2026, 3, 14, 9, 30, tzinfo=NY)
    assert f("03/04/2026") == datetime(2026, 3, 4, 12, 0, tzinfo=NY)
    assert f("03/04/2026", "dmy") == datetime(2026, 4, 3, 12, 0, tzinfo=NY)
    assert f("25/12/2026") == datetime(2026, 12, 25, 12, 0, tzinfo=NY)  # 25 can only be the day
    assert f("2026-02-30") is None and f("13/13/2026") is None and f("no date here, room 12:30") is None
    assert f("1.5.2026") is None  # dotted forms are not recognised, not guessed
    assert f("then 2026-04-01 and earlier 2026-03-01").month == 4  # first one in the text, not the earliest


def test_chat_line_formats_and_invalid_dates():
    p = lambda s: parse_chat_line(s, NY, "mdy")
    assert p("[3/14/26, 10:05:33 PM] Alex: Leaving now")[1:] == ("Alex", "Leaving now")
    assert p("3/14/26, 22:05 - Sam Q: ok")[0] == datetime(2026, 3, 14, 22, 5, tzinfo=NY)
    assert p("[2026-03-14] Alex: hi")[0] == datetime(2026, 3, 14, 12, 0, tzinfo=NY)
    assert p("[3/14/26, 10:05 PM] Alex: narrow no-break spaces")[0].hour == 22
    assert p("[2/30/26, 10:05 PM] Alex: impossible date") is None
    assert p("just a continuation line") is None


# ---------- text / chat ----------
def test_whatsapp_export_becomes_events_with_line_proof(authed):
    cid = make_case(authed)
    chat = ("[3/14/26, 9:58:01 PM] Alex: I'm at home all evening\n"
            "[3/14/26, 10:05:33 PM] Sam: Where are you?\n"
            "still typing this on a second line\n"
            "\n"
            "[3/14/26, 10:07:00 PM] Alex: At the gas station on Route 9\n").encode()
    doc = upload(authed, cid, "chat.txt", chat, "text/plain").json()
    assert doc_status(authed, cid, doc) == "parsed"
    events = timeline(authed, cid)
    assert [e["actors"] for e in events] == [["Alex"], ["Sam"], ["Alex"]]
    last = events[-1]
    assert last["source"]["locator"] == "line 5"  # blank line kept, so the locator matches the real file line
    proof = authed.get(f"{API}/cases/{cid}/proofs/{last['source']['line_id']}").json()
    assert proof["lines"][proof["highlight_index"]]["text"].endswith("gas station on Route 9")
    assert events[0]["occurred_at"].startswith("2026-03-15T01:58:01")  # 9:58 PM New York (EDT) in UTC


def test_plain_text_dates_become_events_and_empty_text_fails(authed):
    cid = make_case(authed)
    doc = upload(authed, cid, "notes.txt", b"Header\nThe vehicle was seen on March 14, 2026 at 10:05 PM.\nNo date.\n").json()
    ev = timeline(authed, cid)
    assert doc_status(authed, cid, doc) == "parsed" and len(ev) == 1 and "vehicle" in ev[0]["title"]
    blank = upload(authed, cid, "blank.txt", b"   \n\n  \n").json()
    assert doc_status(authed, cid, blank) == "failed"


# ---------- pdf ----------
def make_pdf(pages):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for rows in pages:
        for x, y, text in rows:
            c.drawString(x, y, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_text_layer_gives_lines_pages_and_boxes(authed):
    cid = make_case(authed)
    pdf = make_pdf([[(72, 700, "Witness statement of Alex Miller"), (72, 680, "I left on 2026-03-14 at 22:05.")],
                    [(72, 700, "Second page: the stop occurred on March 15, 2026.")]])
    doc = upload(authed, cid, "statement.pdf", pdf).json()
    assert doc_status(authed, cid, doc) == "parsed"
    events = timeline(authed, cid)
    assert len(events) == 2
    first = events[0]
    assert first["source"]["locator"] == "p. 1" and events[1]["source"]["locator"] == "p. 2"
    proof = authed.get(f"{API}/cases/{cid}/proofs/{first['source']['line_id']}").json()
    line = proof["lines"][proof["highlight_index"]]
    x0, y0, x1, y1 = line["bbox"]
    assert "2026-03-14" in line["text"] and 60 <= x0 <= 80 and x1 > x0 and y1 > y0
    assert 792 - 700 - 15 <= y0 <= 792 - 680 + 5 and proof["page"] == 1  # top-left origin: y measured from page top


def test_garbage_pdf_fails_cleanly(authed):
    cid = make_case(authed)
    doc = upload(authed, cid, "broken.pdf", b"%PDF-1.4\nthis is not a real pdf at all").json()
    assert doc_status(authed, cid, doc) in ("failed", "awaiting_extraction")
    assert authed.get(f"{API}/cases/{cid}/audit/verify").json()["valid"] is True


# ---------- OCR ----------
needs_tesseract = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")


def make_png(text):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 220), "white")
    ImageDraw.Draw(img).text((40, 60), text, fill="black", font=ImageFont.load_default(size=48))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@needs_tesseract
def test_image_ocr_returns_line_boxes_confidence_and_events(authed):
    cid = make_case(authed)
    doc = upload(authed, cid, "scan.png", make_png("Receipt dated March 14, 2026 at 10:05 PM"), "image/png").json()
    assert doc_status(authed, cid, doc) == "parsed"
    d = authed.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()
    assert d["ocr_confidence"] and d["ocr_confidence"] > 50
    ev = timeline(authed, cid)
    assert len(ev) == 1 and ev[0]["occurred_at"].startswith("2026-03-15T02:05")
    proof = authed.get(f"{API}/cases/{cid}/proofs/{ev[0]['source']['line_id']}").json()
    assert proof["lines"][0]["bbox"] and proof["confidence"] > 50


@needs_tesseract
@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler not installed")
def test_scanned_pdf_is_rasterised_and_read(authed):
    from PIL import Image

    img = Image.open(io.BytesIO(make_png("Statement signed on March 14, 2026")))
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)  # image-only PDF: no text layer
    cid = make_case(authed)
    doc = upload(authed, cid, "scanned.pdf", buf.getvalue()).json()
    assert doc_status(authed, cid, doc) == "parsed"
    assert len(timeline(authed, cid)) == 1


@needs_tesseract
def test_image_without_text_fails_instead_of_inventing_text(authed):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buf, "PNG")
    cid = make_case(authed)
    doc = upload(authed, cid, "blank.png", buf.getvalue(), "image/png").json()
    assert doc_status(authed, cid, doc) == "failed" and timeline(authed, cid) == []


# ---------- audio ----------
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt "


def test_audio_without_an_engine_falls_back_to_honest_metadata(authed, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # simulate "not installed"
    cid = make_case(authed)
    doc = upload(authed, cid, "call.wav", WAV, "audio/wav").json()
    assert doc_status(authed, cid, doc) == "awaiting_extraction"
    ev = timeline(authed, cid)
    proof = authed.get(f"{API}/cases/{cid}/proofs/{ev[0]['source']['line_id']}").json()
    assert any("transcription engine" in ln["text"] for ln in proof["lines"])


def test_audio_transcription_lines_carry_timestamps(authed, monkeypatch):
    class Seg:
        def __init__(self, start, text, lp):
            self.start, self.text, self.avg_logprob = start, text, lp

    class FakeModel:
        def __init__(self, *a, **k): ...
        def transcribe(self, path, **k):
            return iter([Seg(3.2, "I was at home on 2026-03-14.", -0.1), Seg(61.5, "Then I drove away.", -0.4)]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    cid = make_case(authed)
    doc = upload(authed, cid, "call.wav", WAV, "audio/wav").json()
    assert doc_status(authed, cid, doc) == "transcribed"
    ev = timeline(authed, cid)
    assert len(ev) == 1 and ev[0]["source"]["locator"] == "00:03"
    proof = authed.get(f"{API}/cases/{cid}/proofs/{ev[0]['source']['line_id']}").json()
    assert proof["lines"][0]["start_sec"] == 3.2 and proof["lines"][1]["start_sec"] == 61.5


def test_provider_options_are_validated():
    with pytest.raises(ValueError):
        LocalProvider(date_order="ymd")
    with pytest.raises(ValueError):
        LocalProvider(ocr_lang="eng; rm -rf /")
