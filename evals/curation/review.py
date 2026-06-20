"""Import explicitly attested human reviews. No automatic reviewer identities or accept decisions."""

import csv
import fcntl
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from evals.curation.build import canonical
from evals.curation.validate import review_gate, rows, validate


def import_reviews(directory: Path, sheet: Path) -> int:
    validate(directory)
    if sheet.stat().st_size > 5_000_000:
        raise ValueError("Review sheet exceeds size bound")
    with sheet.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {
            "case_id",
            "case_sha256",
            "reviewer_id",
            "decision",
            "human_attestation",
            "rubric_version",
            "reviewed_at",
            "notes",
        }
        if set(reader.fieldnames or []) != required:
            raise ValueError("Unexpected review sheet columns")
        incoming: list[dict[str, Any]] = []
        for raw in reader:
            if not raw["decision"]:
                continue  # A blank export row is not a review.
            if raw["human_attestation"].strip().lower() != "true":
                raise ValueError("A review requires explicit human attestation")
            incoming.append({**raw, "human_attestation": True})
    target = directory / "reviews.jsonl"
    # Prevent simultaneous imports from overwriting each other's reviews.
    with (directory / ".reviews.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = rows(target) if target.exists() else []
        records = {(r["case_id"], r["reviewer_id"]): r for r in existing}
        added = 0
        for item in incoming:
            key = (item["case_id"], item["reviewer_id"])
            if key in records:
                if records[key] != item:
                    raise ValueError(
                        "An existing review cannot be silently replaced; revise in a new dataset version"
                    )
            else:
                records[key] = item
                added += 1
        combined = [records[k] for k in sorted(records)]
        review_gate(rows(directory / "cases.jsonl"), combined)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("w", dir=directory, encoding="utf-8", delete=False) as output:
                temporary = Path(output.name)
                output.write("".join(canonical(row) + "\n" for row in combined))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return added
