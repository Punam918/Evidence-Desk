"""Local dataset preparation and blinded human review files. Never calls a model."""

import argparse
import csv
import html
import json
from pathlib import Path

from evals.curation.build import ROOT, build, sha
from evals.curation.review import import_reviews
from evals.curation.validate import case_hash, double_review_sample, rows, validate


def export_review(directory: Path, output: Path) -> None:
    validate(directory)
    cases = rows(directory / "cases.jsonl")
    if output.exists():
        raise ValueError("Review output exists; choose a new destination")
    output.mkdir(parents=True)
    second = double_review_sample(cases)
    with (output / "review-sheet.csv").open("w", newline="", encoding="utf-8") as stream:
        columns = [
            "case_id",
            "case_sha256",
            "reviewer_id",
            "decision",
            "human_attestation",
            "rubric_version",
            "reviewed_at",
            "notes",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    "case_id": case["case_id"],
                    "case_sha256": case_hash(case),
                    "rubric_version": "human-review-v1",
                }
            )
    content = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvidenceDesk draft label review</title><style>body{max-width:65rem;margin:2rem auto;padding:1rem;font:18px/1.5 system-ui}article{border-top:1px solid #ccc;margin-top:2rem}pre{white-space:pre-wrap}code{overflow-wrap:anywhere}</style><h1>Draft label review</h1><p>All labels are AI-authored drafts. Review source support, completeness, scope and leakage. No candidate model identity or outputs are shown. Record independent reviews in separate sheets.</p>'
    ]
    manifest = json.loads((directory / "manifest.json").read_text())
    sources = {d["id"]: (directory / d["path"]).read_text() for d in manifest["documents"]}
    for case in cases:
        content.append(
            f'<article id="{html.escape(case["case_id"], quote=True)}"><h2>{html.escape(case["case_id"])}</h2><p>{html.escape(case["query"])}</p><p>Split: {case["split"]}; category: {case["category"]}; selected for independent second review: {case["case_id"] in second}</p>'
        )
        for source_id in case["scope"]["source_ids"]:
            content.append(
                f"<h3>{html.escape(source_id)}</h3><pre>{html.escape(sources[source_id])}</pre>"
            )
        content.append(
            "<details><summary>Proposed labels — unreviewed</summary><pre>"
            + html.escape(
                json.dumps(
                    {
                        k: case[k]
                        for k in (
                            "answerable",
                            "gold_spans",
                            "expected_claims",
                            "abstention_reason",
                        )
                    },
                    indent=2,
                )
            )
            + "</pre></details></article>"
        )
    (output / "review.html").write_text("\n".join(content) + "</html>\n")
    (output / "second-review-sample.json").write_text(json.dumps(sorted(second), indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["build", "validate", "export-review", "import-reviews", "freeze"]
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/benchmark-v1-draft")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sheet", type=Path)
    parser.add_argument("--single-reviewer-limitation", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        build(args.dataset)
    elif args.command == "export-review":
        if args.output is None:
            parser.error("export-review requires --output")
        export_review(args.dataset, args.output)
    elif args.command == "import-reviews":
        if args.sheet is None:
            parser.error("import-reviews requires --sheet")
        import_reviews(args.dataset, args.sheet)
    result = validate(args.dataset, single_reviewer=args.single_reviewer_limitation)
    if args.command == "freeze":
        if not result["promotion_eligible"]:
            parser.exit(
                2, "Freeze refused: missing human reviews, unresolved disputes or second reviews.\n"
            )
        if args.output is None or args.output.exists():
            parser.error("freeze requires a new --output file")
        # A separate immutable approval envelope binds exact labels and reviews, never rewrites draft history.
        result["reviews_sha256"] = sha((args.dataset / "reviews.jsonl").read_bytes())
        result["status"] = "human_reviewed_freeze"
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    summary = {
        **result,
        "human_review": {
            k: v for k, v in result["human_review"].items() if not k.startswith("missing_")
        },
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
