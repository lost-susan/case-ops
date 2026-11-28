"""Local extraction provider: turns an uploaded file into proof lines and timeline events without any cloud service.

  txt  -> every physical line is a proof line; WhatsApp/chat exports also yield one event per timestamped message
  pdf  -> text layer via pdfminer.six (exact line boxes, PDF points, origin top-left); scanned PDFs are rasterised with
          `pdftoppm` and read with Tesseract when both are installed
  jpg/png -> Tesseract OCR (line boxes in pixels, per-line confidence)
  wav/mp3 -> faster-whisper transcription when installed (one line per segment with its start time)

An engine that is not installed never produces invented text: the document falls back to metadata-only proof lines and
stays `awaiting_extraction` with a line saying why. Events only come from explicit dates found in the extracted text."""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_engine.pipelines import dates
from ai_engine.pipelines.ingestion import ExtractedEvent, ExtractedLine, ExtractionResult

log = logging.getLogger("caseops.extraction")

MAX_LINES = 50_000
MAX_PDF_PAGES = 500
MAX_OCR_PDF_PAGES = 50
MAX_EVENTS = 2_000
MAX_IMAGE_PIXELS = 50_000_000
OCR_TIMEOUT_SECONDS = 120
RASTER_TIMEOUT_SECONDS = 300
_LANG_RE = re.compile(r"^[a-zA-Z_]{2,12}(\+[a-zA-Z_]{2,12}){0,3}$")
_WS = {ord(" "): " ", ord(" "): " ", ord("‎"): None, ord("‏"): None}

_CHAT_PATTERNS = (
    # [3/14/26, 10:05:33 PM] Alex: text
    re.compile(r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?)\]\s+"
               r"(?P<who>[^:]{1,80}):\s?(?P<msg>.*)$"),
    # 3/14/26, 22:05 - Alex: text
    re.compile(r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?)\s+-\s+"
               r"(?P<who>[^:]{1,80}):\s?(?P<msg>.*)$"),
    # [2026-03-14 22:05] Alex: text   |   2026-03-14 Alex: text
    re.compile(r"^\[?(?P<iso>\d{4}-\d{2}-\d{2})(?:[T ,]+(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?\]?\s+[-–]?\s*"
               r"(?P<who>[^:\[\]]{1,60}):\s?(?P<msg>.*)$"),
)


def _clean(s: str) -> str:
    return s.translate(_WS)


def metadata_result(reason: str, *, kind: str, filename: str, sha256: str, size_bytes: int,
                    received_at: datetime) -> ExtractionResult:
    """Honest fallback: proof lines describe the file itself and say why nothing was extracted."""
    lines = (
        ExtractedLine(f"FILE: {filename}"),
        ExtractedLine(f"TYPE: {kind.upper()} • SIZE: {size_bytes} bytes"),
        ExtractedLine(f"SHA-256: {sha256}"),
        ExtractedLine(reason),
    )
    event = ExtractedEvent(title=f"Ingested: {filename}", occurred_at=received_at, line_index=0)
    return ExtractionResult(status="awaiting_extraction", lines=lines, events=(event,), confidence=None)


def events_from_lines(lines: list[ExtractedLine], tz: ZoneInfo, date_order: str,
                      skip: set[int] | None = None) -> tuple[ExtractedEvent, ...]:
    out: list[ExtractedEvent] = []
    for i, ln in enumerate(lines):
        if skip and i in skip:
            continue
        found = dates.find_first(_clean(ln.text), tz, date_order)
        if found is None:
            continue
        title = " ".join(ln.text.split())[:200]
        out.append(ExtractedEvent(title=title, occurred_at=found, line_index=i))
        if len(out) >= MAX_EVENTS:
            break
    return tuple(out)


def parse_chat_line(raw: str, tz: ZoneInfo, date_order: str) -> tuple[datetime, str, str] | None:
    """(timestamp, speaker, message) for a chat-export line, or None if it is not one / its date is invalid."""
    text = _clean(raw).strip()
    for rx in _CHAT_PATTERNS:
        m = rx.match(text)
        if not m:
            continue
        gd = m.groupdict()
        if gd.get("iso"):
            y, mo, d = (int(x) for x in gd["iso"].split("-"))
            day = dates.to_date(y, mo, d)
        else:
            a, b, y = gd["date"].split("/")
            day = dates.numeric_date(int(a), int(b), y, date_order)
        if day is None:
            return None
        t = dates.parse_clock_text(gd["time"]) if gd.get("time") else None
        if gd.get("time") and t is None:
            return None
        return dates.localize(day, t, tz), gd["who"].strip(), gd["msg"].strip()
    return None


def extract_text(path: Path, tz: ZoneInfo, date_order: str) -> ExtractionResult:
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    texts = raw.splitlines()[:MAX_LINES]  # blank lines are kept so "line N" always matches the file
    lines = [ExtractedLine(t) for t in texts]
    chat = [(i, parse_chat_line(t, tz, date_order)) for i, t in enumerate(texts) if t.strip()]
    hits = [(i, c) for i, c in chat if c]
    if len(hits) >= 3 and len(hits) >= 0.3 * max(1, len(chat)):
        events = tuple(
            ExtractedEvent(title=f"{who}: {msg}"[:200] if msg else f"{who} (message)", occurred_at=when,
                           line_index=i, actors=(who[:120],))
            for i, (when, who, msg) in hits[:MAX_EVENTS]
        )
    else:
        events = events_from_lines(lines, tz, date_order)
    if not any(t.strip() for t in texts):
        raise ValueError("Text file contains no text")
    return ExtractionResult(status="parsed", lines=tuple(lines), events=events, confidence=None)


def extract_pdf_text_layer(path: Path) -> list[ExtractedLine]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextContainer, LTTextLine

    out: list[ExtractedLine] = []
    for page_no, page in enumerate(extract_pages(str(path), laparams=LAParams()), start=1):
        if page_no > MAX_PDF_PAGES:
            break
        height = page.height
        for box in page:
            if not isinstance(box, LTTextContainer):
                continue
            for tl in box:
                if not isinstance(tl, LTTextLine):
                    continue
                text = " ".join(tl.get_text().split())
                if not text:
                    continue
                x0, y0, x1, y1 = tl.bbox  # PDF origin is bottom-left; store top-left like screen coordinates
                out.append(ExtractedLine(text, page=page_no,
                                         bbox=(round(x0), round(height - y1), round(x1), round(height - y0))))
                if len(out) >= MAX_LINES:
                    return out
    return out


def _check_lang(lang: str) -> str:
    if not _LANG_RE.match(lang):
        raise ValueError("Invalid OCR language setting")
    return lang


def ocr_image(image, page: int, lang: str) -> tuple[list[ExtractedLine], list[float]]:
    """Tesseract word boxes grouped into lines. Returns the lines and every word confidence (0-100)."""
    import pytesseract

    data = pytesseract.image_to_data(image, lang=_check_lang(lang), output_type=pytesseract.Output.DICT,
                                     timeout=OCR_TIMEOUT_SECONDS)
    groups: dict[tuple, dict] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top, w, h = (int(data[k][i]) for k in ("left", "top", "width", "height"))
        g = groups.setdefault(key, {"words": [], "box": [left, top, left + w, top + h], "conf": []})
        g["words"].append(word)
        g["conf"].append(conf)
        g["box"] = [min(g["box"][0], left), min(g["box"][1], top), max(g["box"][2], left + w),
                    max(g["box"][3], top + h)]
        confs.append(conf)
    lines = [ExtractedLine(" ".join(g["words"]), page=page, bbox=tuple(g["box"]),
                           confidence=round(sum(g["conf"]) / len(g["conf"]), 1))
             for g in groups.values()]
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    return lines, confs


def _open_image(path: Path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    img = Image.open(path)
    img.load()
    return img


def extract_image(path: Path, tz: ZoneInfo, date_order: str, lang: str) -> ExtractionResult:
    img = _open_image(path)
    lines, confs = ocr_image(img, 1, lang)
    if not lines:
        raise ValueError("OCR found no text in the image")
    return ExtractionResult(status="parsed", lines=tuple(lines), events=events_from_lines(lines, tz, date_order),
                            confidence=round(sum(confs) / len(confs), 1))


def extract_pdf(path: Path, tz: ZoneInfo, date_order: str, lang: str) -> ExtractionResult:
    lines = extract_pdf_text_layer(path)
    confidence: float | None = None
    if not lines:  # no text layer: a scan. Rasterise and OCR when the tools exist.
        if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
            raise EngineUnavailable("This PDF has no text layer and needs OCR, but pdftoppm/tesseract are not installed.")
        confs: list[float] = []
        with tempfile.TemporaryDirectory(prefix="caseops-ocr-") as tmp:
            subprocess.run(["pdftoppm", "-r", "200", "-png", "-l", str(MAX_OCR_PDF_PAGES), str(path),
                            str(Path(tmp) / "p")], check=True, timeout=RASTER_TIMEOUT_SECONDS,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for page_no, png in enumerate(sorted(Path(tmp).glob("p-*.png")), start=1):
                page_lines, page_confs = ocr_image(_open_image(png), page_no, lang)
                lines.extend(page_lines)
                confs.extend(page_confs)
        if not lines:
            raise EngineUnavailable("OCR found no readable text in this scanned PDF.")
        confidence = round(sum(confs) / len(confs), 1)
    return ExtractionResult(status="parsed", lines=tuple(lines[:MAX_LINES]),
                            events=events_from_lines(lines[:MAX_LINES], tz, date_order), confidence=confidence)


def extract_audio(path: Path, tz: ZoneInfo, date_order: str, model_size: str) -> ExtractionResult:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise EngineUnavailable("No transcription engine is installed (faster-whisper).") from exc
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(path), vad_filter=True)
    lines: list[ExtractedLine] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        conf = max(0.0, min(100.0, math.exp(seg.avg_logprob) * 100)) if seg.avg_logprob is not None else None
        lines.append(ExtractedLine(text, start_sec=float(seg.start), confidence=None if conf is None else round(conf, 1)))
        if len(lines) >= MAX_LINES:
            break
    if not lines:
        raise EngineUnavailable("Transcription produced no speech.")
    scored = [ln.confidence for ln in lines if ln.confidence is not None]
    return ExtractionResult(status="transcribed", lines=tuple(lines), events=events_from_lines(lines, tz, date_order),
                            confidence=round(sum(scored) / len(scored), 1) if scored else None)


class EngineUnavailable(Exception):
    """The engine needed for this file is missing (or found nothing); fall back to honest metadata-only proof."""


class LocalProvider:
    def __init__(self, date_order: str = "mdy", whisper_model: str = "base", ocr_lang: str = "eng"):
        if date_order not in ("mdy", "dmy"):
            raise ValueError("date_order must be 'mdy' or 'dmy'")
        self.date_order, self.whisper_model, self.ocr_lang = date_order, whisper_model, _check_lang(ocr_lang)

    def process(self, *, path: Path, kind: str, filename: str, sha256: str, size_bytes: int, received_at: datetime,
                case_timezone: str = "UTC") -> ExtractionResult:
        tz = ZoneInfo(case_timezone)
        try:
            if kind == "txt":
                return extract_text(path, tz, self.date_order)
            if kind == "pdf":
                return extract_pdf(path, tz, self.date_order, self.ocr_lang)
            if kind in ("jpg", "png"):
                if not shutil.which("tesseract"):
                    raise EngineUnavailable("No OCR engine is installed (tesseract).")
                return extract_image(path, tz, self.date_order, self.ocr_lang)
            if kind in ("wav", "mp3"):
                return extract_audio(path, tz, self.date_order, self.whisper_model)
        except EngineUnavailable as exc:
            log.warning("Extraction fell back to metadata for %s: %s", filename, exc)
            return metadata_result(str(exc), kind=kind, filename=filename, sha256=sha256, size_bytes=size_bytes,
                                   received_at=received_at)
        raise ValueError(f"Unsupported kind {kind!r}")
