import argparse
import json
from pathlib import Path

from evals.curation.build import ROOT
from evals.runner.export import imports
from evals.runner.recorded import aggregate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate hash-bound recorded retrieval; no inference calls"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/benchmark-v1-draft")
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["development", "test"], default="development")
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--allow-draft-diagnostic", action="store_true")
    parser.add_argument("--imports-dir", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--hardware")
    args = parser.parse_args()
    if args.imports_dir and (not args.source_manifest or not args.hardware):
        parser.error("Imports require --source-manifest and --hardware")
    if args.imports_dir and args.imports_dir.exists():
        parser.error("Imports directory exists; choose a new destination")
    if args.output.exists():
        parser.error("Output exists; choose a new immutable report path")
    result = aggregate(
        args.dataset,
        args.observations,
        split=args.split,
        freeze=args.freeze,
        allow_draft_diagnostic=args.allow_draft_diagnostic,
    )
    artifact = (json.dumps(result, indent=2) + "\n").encode()
    exported = (
        imports(result, artifact, args.source_manifest, args.hardware) if args.imports_dir else {}
    )
    with args.output.open("xb") as stream:
        stream.write(artifact)
    if args.imports_dir:
        args.imports_dir.mkdir()
        for variant, value in exported.items():
            with (args.imports_dir / f"{variant}.json").open("x") as stream:
                stream.write(value.model_dump_json(indent=2) + "\n")
    print(
        json.dumps(
            {"status": result["status"], "scope": result["scope"], "promotion_eligible": False}
        )
    )


if __name__ == "__main__":
    main()
