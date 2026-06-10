"""Deterministic format extraction. Invoke only within the isolated parser process."""

import csv
import hashlib
import io
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from evidencedesk.errors import Problem
from evidencedesk.ingestion.blocks import Block, BlocksManifest, SourceLocator
from evidencedesk.ingestion.normalize import normalize_text
from evidencedesk.ingestion.validation import validate_file

MAX_EXTRACTED = 100 * 1024 * 1024


class Builder:
    def __init__(self, sha: str, mime: str):
        self.sha, self.mime = sha, mime
        self.blocks: list[Block] = []
        self.position = 0
        self.bytes = 0
        self.headings: list[str] = []
        self.images = 0
        self.pages: int | None = None
        self.warnings: list[str] = []

    def add(
        self,
        value: str,
        locator: SourceLocator,
        kind: str = "paragraph",
        *,
        header: str | None = None,
        language: str | None = None,
    ) -> None:
        self.bytes += len(value.encode())
        if self.bytes > MAX_EXTRACTED:
            raise Problem(422, "extraction_limit", "Extracted content exceeds 100 MiB.")
        text = normalize_text(value)
        if not text.strip():
            return
        if len(self.blocks) >= 10000:
            raise Problem(422, "block_limit", "The document exceeds 10,000 source blocks.")
        block = Block.model_validate(
            {
                "ordinal": len(self.blocks),
                "kind": kind,
                "original_text": value,
                "text": text,
                "char_start": self.position,
                "char_end": self.position + len(text),
                "heading_path": self.headings.copy(),
                "locator": locator,
                "header": header,
                "language": language,
            }
        )
        self.blocks.append(block)
        self.position = block.char_end + 2

    def heading(self, text: str, level: int) -> None:
        self.headings = self.headings[: level - 1] + [normalize_text(text)]

    def finish(self) -> BlocksManifest:
        if not self.blocks:
            raise Problem(422, "empty_document", "The document contains no supported text.")
        if self.images:
            self.warnings.append(
                f"{self.images} embedded image(s) omitted; image content is not searchable."
            )
        return BlocksManifest(
            original_sha256=self.sha,
            mime_type=self.mime,
            blocks=self.blocks,
            page_count=self.pages,
            omitted_images=self.images,
            warnings=self.warnings,
            text_sha256=hashlib.sha256(
                "\n\n".join(b.text for b in self.blocks).encode()
            ).hexdigest(),
        )


def text_blocks(builder: Builder, text: str, markdown: bool) -> None:
    lines = text.splitlines(keepends=True)
    buffer: list[str] = []
    first = 1
    fence: str | None = None
    language: str | None = None

    def flush(end: int) -> None:
        if buffer:
            builder.add(
                "".join(buffer),
                SourceLocator(
                    kind="markdown" if markdown else "text",
                    index=first - 1,
                    line_start=first,
                    line_end=end,
                ),
                "code" if fence else "paragraph",
                language=language,
            )
            buffer.clear()

    for number, line in enumerate(lines, 1):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})([^\n]*)", line) if markdown else None
        if marker:
            if fence is None:
                flush(number - 1)
                first, fence, language = number, marker[1], marker[2].strip()[:80] or None
                buffer.append(line)
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                buffer.append(line)
                flush(number)
                fence, language = None, None
            else:
                buffer.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown and not fence else None
        if heading:
            flush(number - 1)
            builder.heading(heading[2], len(heading[1]))
            builder.add(
                line.rstrip("\r\n"),
                SourceLocator(
                    kind="markdown", index=number - 1, line_start=number, line_end=number
                ),
                "heading",
            )
        elif not line.strip() and not fence:
            flush(number - 1)
        else:
            if not buffer:
                first = number
            buffer.append(line)
    flush(len(lines))
    if fence:
        builder.warnings.append("An unclosed Markdown code fence was preserved as source text.")


def pdf_blocks(builder: Builder, path: Path) -> None:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=True)
    if reader.is_encrypted:
        raise Problem(422, "encrypted_pdf", "Encrypted PDFs are unsupported.")
    builder.pages = len(reader.pages)
    if not 1 <= builder.pages <= 200:
        raise Problem(422, "page_limit", "PDFs must contain between 1 and 200 pages.")
    skipped = []
    for number, page in enumerate(reader.pages, 1):
        value = page.extract_text() or ""
        # Enumerate XObject metadata only: do not decode image pixels.
        resources = page.get("/Resources", {})
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobjects = resources.get("/XObject", {})
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        builder.images += sum(
            1 for obj in xobjects.values() if obj.get_object().get("/Subtype") == "/Image"
        )
        if len("".join(value.split())) < 20:
            skipped.append(number)
        builder.add(value, SourceLocator(kind="pdf", index=number - 1, page=number))
    if skipped:
        code = "ocr_required" if len(skipped) > builder.pages / 2 else "partial_coverage"
        raise Problem(
            422,
            code,
            "Some PDF pages have insufficient extractable text; complete text coverage is required.",
        )
    builder.warnings.append(
        "PDF text order follows the extractor; complex layouts and tables may require review."
    )


def docx_blocks(builder: Builder, path: Path) -> None:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    builder.images = len(document.inline_shapes)
    for index, element in enumerate(document.iter_inner_content()):
        if isinstance(element, Paragraph):
            style = element.style.name if element.style else ""
            heading = re.fullmatch(r"Heading ([1-6])", style)
            if heading:
                builder.heading(element.text, int(heading[1]))
            builder.add(
                element.text,
                SourceLocator(kind="docx", index=index),
                "heading" if heading else "paragraph",
            )
        elif isinstance(element, Table):
            rows = element.rows
            if not rows:
                continue
            headers = [cell.text for cell in rows[0].cells]
            for row_no, row in enumerate(rows, 1):
                for col_no, cell in enumerate(row.cells, 1):
                    builder.add(
                        cell.text,
                        SourceLocator(kind="docx", index=index, row=row_no, column=col_no),
                        "table_cell",
                        header=headers[col_no - 1]
                        if row_no > 1 and col_no <= len(headers)
                        else None,
                    )
    builder.warnings.append(
        "DOCX pagination is unavailable; headers, footers, comments and floating objects are not included."
    )


def html_blocks(builder: Builder, value: str) -> None:
    from bs4 import BeautifulSoup
    from bs4.element import Comment, NavigableString, Tag

    soup = BeautifulSoup(value, "lxml")
    builder.images = len(soup.find_all("img"))
    for tag in soup.find_all(
        ["script", "style", "noscript", "template", "iframe", "object", "svg"]
    ):
        tag.decompose()
    index = 0

    def walk(tag: Tag) -> None:
        nonlocal index
        buffer: list[str] = []

        def flush() -> None:
            nonlocal index
            if "".join(buffer).strip():
                builder.add("".join(buffer), SourceLocator(kind="html", index=index))
                index += 1
            buffer.clear()

        for child in tag.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                buffer.append(str(child))
            elif isinstance(child, Tag):
                if child.name == "table":
                    flush()
                    headers: list[str] = []
                    for row_no, row in enumerate(child.find_all("tr"), 1):
                        if row.find_parent("table") is not child:
                            continue
                        cells = row.find_all(["td", "th"], recursive=False)
                        if len(cells) > 1000:
                            raise Problem(
                                422, "table_limit", "Tables support at most 1,000 columns."
                            )
                        if row_no == 1:
                            headers = [cell.get_text("") for cell in cells]
                        for col_no, cell in enumerate(cells, 1):
                            builder.add(
                                cell.get_text(""),
                                SourceLocator(kind="html", index=index, row=row_no, column=col_no),
                                "table_cell",
                                header=headers[col_no - 1]
                                if row_no > 1 and col_no <= len(headers)
                                else None,
                            )
                    index += 1
                elif child.name in (
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "p",
                    "pre",
                    "li",
                    "td",
                    "th",
                ):
                    flush()
                    text = child.get_text("", strip=False)
                    heading = child.name.startswith("h") and child.name != "th"
                    if heading:
                        builder.heading(text, int(child.name[1]))
                    builder.add(
                        text,
                        SourceLocator(kind="html", index=index),
                        "heading" if heading else "code" if child.name == "pre" else "paragraph",
                    )
                    index += 1
                elif child.name in (
                    "div",
                    "section",
                    "article",
                    "main",
                    "body",
                    "table",
                    "tr",
                    "ul",
                    "ol",
                    "blockquote",
                    "header",
                    "footer",
                    "html",
                ):
                    flush()
                    walk(child)
                else:
                    buffer.append(child.get_text(""))
        flush()

    walk(soup.body or soup)


def csv_blocks(builder: Builder, value: str) -> None:
    csv.field_size_limit(20 * 1024 * 1024)
    reader = csv.reader(io.StringIO(value, newline=""), strict=True)
    headers: list[str] = []
    for row_no, row in enumerate(reader, 1):
        if len(row) > 1000:
            raise Problem(422, "table_limit", "Tables support at most 1,000 columns.")
        if row_no == 1:
            headers = row
        for col_no, cell in enumerate(row, 1):
            builder.add(
                cell,
                SourceLocator(kind="csv", index=row_no - 1, row=row_no, column=col_no),
                "table_cell",
                header=headers[col_no - 1] if row_no > 1 and col_no <= len(headers) else None,
            )


def json_blocks(builder: Builder, value: str) -> None:
    # Bound nesting before json.loads allocates a tree, ignoring braces inside strings.
    depth, quoted, escaped = 0, False, False
    for char in value:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 20:
                raise Problem(422, "json_depth", "JSON depth exceeds 20 levels.")
        elif char in "]}":
            depth -= 1

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise Problem(
                    422, "duplicate_json_key", "Duplicate JSON keys would lose source content."
                )
            result[key] = item
        return result

    def invalid_number(value: str) -> None:
        raise Problem(422, "invalid_json", "JSON must use finite numbers.")

    parsed = json.loads(
        value, object_pairs_hook=pairs, parse_constant=invalid_number, parse_float=Decimal
    )
    entries = 0

    def visit(node: Any, pointer: str) -> None:
        nonlocal entries
        if isinstance(node, dict):
            for key, item in node.items():
                visit(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, pointer + "/" + str(index))
        else:
            entries += 1
            if entries > 50000:
                raise Problem(422, "json_entries", "JSON exceeds 50,000 scalar entries.")
            rendered = (
                str(node)
                if isinstance(node, (str, Decimal))
                else json.dumps(node, ensure_ascii=False, allow_nan=False)
            )
            builder.add(
                rendered,
                SourceLocator(kind="json", index=entries - 1, pointer=pointer),
                "value",
                header=pointer,
            )

    visit(parsed, "")


def extract(path: Path, title: str, mime: str, sha256: str) -> BlocksManifest:
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
        raise Problem(422, "object_checksum_mismatch", "Original file checksum does not match.")
    validated = validate_file(path, title, mime)
    builder = Builder(sha256, validated)
    suffix = Path(title).suffix.lower()
    try:
        if suffix == ".pdf":
            pdf_blocks(builder, path)
        elif suffix == ".docx":
            docx_blocks(builder, path)
        else:
            value = path.read_text(encoding="utf-8-sig")
            if suffix in (".html", ".htm"):
                html_blocks(builder, value)
            elif suffix == ".csv":
                csv_blocks(builder, value)
            elif suffix == ".json":
                json_blocks(builder, value)
            else:
                text_blocks(builder, value, suffix == ".md")
        return builder.finish()
    except Problem:
        raise
    except Exception as exc:
        raise Problem(422, "parse_failed", "The file could not be safely extracted.") from exc
