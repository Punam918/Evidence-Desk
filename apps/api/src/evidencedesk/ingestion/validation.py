"""Bounded upload validation. Full parsing happens only in the isolated ingest worker."""

import codecs
import re
import stat
import struct
import zipfile
from pathlib import Path

from evidencedesk.errors import Problem

MAX_BYTES = 20 * 1024 * 1024
MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
}


def display_name(filename: str | None) -> str:
    name = re.split(r"[/\\]", filename or "")[-1]
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
    if not name or len(name) > 255:
        raise Problem(422, "invalid_filename", "Choose a filename between 1 and 255 characters.")
    return name


def validate_docx(path: Path) -> None:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65557))
        tail = stream.read()
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise Problem(422, "invalid_archive", "DOCX archive directory is invalid.")
    record = struct.unpack_from("<4s4H2LH", tail, offset)
    if record[1] or record[2] or record[3] != record[4] or not 1 <= record[4] <= 1000:
        raise Problem(422, "archive_limits", "DOCX requires a bounded single-volume archive.")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 1000 or len(infos) != record[4]:
                raise Problem(422, "archive_limits", "Too many DOCX archive members.")
            total = 0
            for info in infos:
                parts = info.filename.replace("\\", "/").split("/")
                if (
                    info.filename.startswith(("/", "\\"))
                    or ".." in parts
                    or ":" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1
                ):
                    raise Problem(
                        422,
                        "unsafe_archive",
                        "DOCX contains unsafe paths, links or encrypted members.",
                    )
                if "vbaproject" in info.filename.lower() or info.filename.lower().endswith(
                    (".exe", ".dll", ".js")
                ):
                    raise Problem(
                        422, "unsafe_archive", "Macro or executable office content is unsupported."
                    )
                total += info.file_size
                if total > 100 * 1024 * 1024 or info.file_size > max(1, info.compress_size) * 100:
                    raise Problem(
                        422, "archive_limits", "DOCX expansion exceeds the allowed limits."
                    )
            names = {info.filename for info in infos}
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise Problem(422, "invalid_docx", "The file is not a supported DOCX document.")
    except (zipfile.BadZipFile, NotImplementedError) as exc:
        raise Problem(422, "invalid_archive", "DOCX archive is invalid.") from exc


def validate_file(path: Path, title: str, declared: str | None) -> str:
    suffix = Path(title).suffix.lower()
    if suffix not in MIMES:
        raise Problem(
            422, "unsupported_file_type", "Use PDF, DOCX, UTF-8 text, Markdown, HTML, CSV or JSON."
        )
    if not 1 <= path.stat().st_size <= MAX_BYTES:
        raise Problem(413, "upload_size", "The file must be nonempty and at most 20 MiB.")
    mime = MIMES[suffix]
    allowed = {mime, "application/octet-stream", None, ""}
    if suffix in (".md", ".csv", ".json", ".html", ".htm"):
        allowed.add("text/plain")
    if declared and declared.split(";", 1)[0].strip().lower() not in allowed:
        raise Problem(422, "mime_mismatch", "File type does not match its declared media type.")
    with path.open("rb") as stream:
        prefix = stream.read(1024)
    if suffix == ".pdf":
        if not prefix.startswith(b"%PDF-"):
            raise Problem(422, "mime_mismatch", "This is not a PDF document.")
    elif suffix == ".docx":
        if not prefix.startswith(b"PK\x03\x04"):
            raise Problem(422, "mime_mismatch", "This is not a DOCX document.")
        validate_docx(path)
    else:
        if b"\x00" in prefix or prefix.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04", b"%PDF-")):
            raise Problem(422, "mime_mismatch", "Binary content cannot be uploaded as text.")
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        try:
            with path.open("rb") as stream:
                while part := stream.read(64 * 1024):
                    if b"\x00" in part:
                        raise Problem(
                            422, "mime_mismatch", "Binary content cannot be uploaded as text."
                        )
                    decoder.decode(part)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise Problem(422, "unsupported_encoding", "Text documents must use UTF-8.") from exc
    return mime
