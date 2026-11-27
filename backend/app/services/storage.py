"""Evidence file storage: streamed, size-capped, hashed while writing, content-sniffed, stored under random keys.
The client's filename never touches the filesystem path."""
import codecs
import hashlib
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import BinaryIO

ALLOWED: dict[str, tuple[str, str]] = {  # extension -> (kind, content type we will serve it as)
    "pdf": ("pdf", "application/pdf"),
    "txt": ("txt", "text/plain; charset=utf-8"),
    "wav": ("wav", "audio/wav"),
    "mp3": ("mp3", "audio/mpeg"),
    "jpg": ("jpg", "image/jpeg"),
    "jpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
}
_KEY_RE = re.compile(r"[0-9a-f]{32}")
CHUNK = 1024 * 1024


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def sanitize_filename(name: str) -> str:
    """Display name only: basename, no control chars, bounded length."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip(" .")
    return name[:255]


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def sniff(kind: str, head: bytes) -> bool:
    """Magic-byte check so a renamed file (e.g. .exe -> .pdf) is refused."""
    if kind == "pdf":
        return head.startswith(b"%PDF-")
    if kind == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "jpg":
        return head.startswith(b"\xff\xd8\xff")
    if kind == "wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if kind == "mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if kind == "txt":
        if b"\x00" in head:
            return False
        try:
            codecs.getincrementaldecoder("utf-8")().decode(head, final=False)  # tolerate a cut multibyte tail
        except UnicodeDecodeError:
            return False
        return True
    return False


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("Invalid storage key")
        return self.root / key

    def save(self, fileobj: BinaryIO, *, kind: str, max_bytes: int) -> tuple[str, int, str]:
        """Returns (storage_key, size_bytes, sha256_hex)."""
        key = uuid.uuid4().hex
        final, tmp = self.path(key), self.root / f".{key}.part"
        digest, size, head = hashlib.sha256(), 0, b""
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := fileobj.read(CHUNK):
                    if not head:
                        head = chunk[:65536]
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadError(413, "File is too large")
                    digest.update(chunk)
                    out.write(chunk)
                if size == 0:
                    raise UploadError(422, "File is empty")
                if not sniff(kind, head):
                    raise UploadError(415, "File content does not match its type")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return key, size, digest.hexdigest()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)
