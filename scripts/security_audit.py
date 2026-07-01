"""Reproducible local supply-chain gate. Findings and incomplete scans fail closed."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from evidencedesk.ingestion.parser_protocol import service_fingerprint

if __package__:
    from .application_images import verified_images
else:
    from application_images import verified_images

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs/10-testing/security"
TOOLS = ROOT / ".tools/security"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scanner(name: str, *, bootstrap: bool) -> str:
    lock = json.loads((ROOT / "infra/security/tools.lock.json").read_text())[name]
    binary = TOOLS / name
    if not binary.exists():
        if not bootstrap:
            raise RuntimeError("Pinned scanner missing; run with --bootstrap")
        TOOLS.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
            response = client.get(lock["archive_url"])
            response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != lock["sha256"]:
            raise RuntimeError("Scanner archive hash mismatch")
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "scanner.tar.gz"
            archive.write_bytes(response.content)
            with tarfile.open(archive) as contents:
                member = contents.getmember(name)
                if not member.isfile():
                    raise RuntimeError("Scanner is not a regular archive member")
                contents.extract(member, TOOLS, filter="data")
    if digest(binary) != lock["binary_sha256"]:
        raise RuntimeError("Scanner binary hash mismatch")
    return str(binary)


def blockers(image_report: dict) -> list[dict]:
    result = []
    for target in image_report.get("Results", []):
        for item in target.get("Vulnerabilities", []):
            if item["Severity"] in ("HIGH", "CRITICAL"):
                result.append(
                    {
                        "target": target["Target"],
                        "id": item["VulnerabilityID"],
                        "package": item["PkgName"],
                        "installed": item["InstalledVersion"],
                        "fixed": item.get("FixedVersion"),
                        "severity": item["Severity"],
                    }
                )
    return result


def verified_parser_service(root: Path) -> tuple[str, dict[str, str]]:
    """Bind the exact parser service image to reviewed, currently unchanged build inputs."""
    build_name = "infra/parser/service-build.lock.json"
    source_name = "infra/parser/service.lock.json"
    raw = {}
    for name in (build_name, source_name):
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
            or path.stat().st_size > 2 * 1024 * 1024
        ):
            raise ValueError("Parser service lock missing, symbolic or oversized")
        raw[name] = path.read_bytes()
    build = json.loads(raw[build_name])
    if (
        not isinstance(build, dict)
        or set(build) != {"image_id", "source_sha256"}
        or not isinstance(build["image_id"], str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", build["image_id"])
    ):
        raise ValueError("Parser service requires an exact built image ID")
    fingerprint = service_fingerprint(root / source_name, root)
    if build["source_sha256"] != fingerprint:
        raise ValueError("Parser service build inputs changed; rebuild before release")
    inputs = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
    source = json.loads(raw[source_name])
    for filename, expected in source["files"].items():
        relative = (
            Path("apps/api/src" if filename.startswith("evidencedesk/") else "infra/parser")
            / filename
        )
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Parser service input escaped project root")
        actual = digest(path)
        if actual != expected:
            raise ValueError("Parser service inputs changed during verification")
        inputs[relative.as_posix()] = actual
    if any(digest(root / name) != inputs[name] for name in raw):
        raise ValueError("Parser service locks changed during verification")
    return build["image_id"], inputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap", action="store_true", help="Download only hash-locked scanner binaries"
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    REPORTS.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "started_at_unix": time.time(),
        "status": "incomplete",
        "steps": [],
        "inputs": {
            name: digest(ROOT / name)
            for name in (
                "uv.lock",
                "pnpm-lock.yaml",
                "infra/images.lock",
                "infra/parser/image.lock.json",
                "infra/monitoring/images.lock.json",
                "infra/security/tools.lock.json",
                "infra/security/gitleaks.toml",
                "infra/application/images.lock.json",
            )
        },
        "release_blockers": [],
    }
    manifest_path = REPORTS / "audit-manifest.json"

    def save() -> None:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    save()
    try:
        parser_service_image, parser_service_inputs = verified_parser_service(ROOT)
        manifest["inputs"].update(parser_service_inputs)
        save()
        trivy = scanner("trivy", bootstrap=args.bootstrap)
        gitleaks = scanner("gitleaks", bootstrap=args.bootstrap)

        def run(
            name: str, command: list[str], output: str, *, stdout: bool = False
        ) -> dict | list | None:
            target = REPORTS / output
            target.unlink(
                missing_ok=True
            )  # A failed command must not leave an old successful artifact.
            result = subprocess.run(command, capture_output=True, text=True, timeout=360)
            if stdout:
                target.write_text(result.stdout)
            try:
                data = json.loads(target.read_text())
            except ValueError, OSError:
                data = None
            manifest["steps"].append(
                {
                    "name": name,
                    "exit_code": result.returncode,
                    "report": output,
                    "report_sha256": digest(target) if target.exists() else None,
                }
            )
            if result.returncode != 0 or data is None:
                manifest["release_blockers"].append(
                    {"scan": name, "reason": "scan_failed_or_invalid_report"}
                )
            print(f"{name}: exit {result.returncode}; report {output}", flush=True)
            save()
            return data

        python = run(
            "python-advisories",
            [
                str(ROOT / ".venv/bin/pip-audit"),
                "--path",
                str(ROOT / ".venv/lib/python3.14/site-packages"),
                "--skip-editable",
                "--format",
                "json",
                "--progress-spinner",
                "off",
                "--output",
                str(REPORTS / "python-audit.json"),
            ],
            "python-audit.json",
        )
        if isinstance(python, dict) and python.get("dependencies"):
            for dependency in python.get("dependencies", []):
                if dependency.get("vulns"):
                    manifest["release_blockers"].append(
                        {
                            "scan": "python",
                            "package": dependency["name"],
                            "findings": dependency["vulns"],
                        }
                    )
                if dependency.get("skip_reason"):
                    if (
                        dependency["name"] == "evidencedesk"
                        and dependency["skip_reason"] == "distribution marked as editable"
                    ):
                        continue  # First-party code is covered by security fixtures/source scanning.
                    if (
                        dependency["name"] == "torch"
                        and importlib.metadata.version("torch") == "2.14.1+cpu"
                    ):
                        with tempfile.TemporaryDirectory() as temp:
                            requirement = Path(temp) / "torch.txt"
                            requirement.write_text("torch==2.14.1\n")
                            supplemental = run(
                                "torch-upstream-advisories",
                                [
                                    str(ROOT / ".venv/bin/pip-audit"),
                                    "-r",
                                    str(requirement),
                                    "--no-deps",
                                    "--disable-pip",
                                    "--format",
                                    "json",
                                    "--progress-spinner",
                                    "off",
                                    "--output",
                                    str(REPORTS / "torch-upstream-audit.json"),
                                ],
                                "torch-upstream-audit.json",
                            )
                        if not isinstance(supplemental, dict) or any(
                            d.get("vulns") or d.get("skip_reason")
                            for d in supplemental.get("dependencies", [])
                        ):
                            manifest["release_blockers"].append(
                                {"scan": "torch", "reason": "upstream_not_clean"}
                            )
                    else:
                        manifest["release_blockers"].append(
                            {
                                "scan": "python",
                                "package": dependency["name"],
                                "reason": "unaudited_dependency",
                            }
                        )
        else:
            manifest["release_blockers"].append(
                {"scan": "python", "reason": "empty_dependency_analysis"}
            )
        javascript = run(
            "javascript-advisories",
            [str(ROOT / "scripts/pnpm"), "audit", "--json"],
            "javascript-audit.json",
            stdout=True,
        )
        if (
            not isinstance(javascript, dict)
            or "metadata" not in javascript
            or any(javascript["metadata"].get("vulnerabilities", {}).values())
        ):
            manifest["release_blockers"].append(
                {"scan": "javascript", "reason": "findings_or_incomplete"}
            )
        credentials = run(
            "source-credentials",
            [
                gitleaks,
                "dir",
                ".",
                "--config",
                "infra/security/gitleaks.toml",
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(REPORTS / "credentials.json"),
                "--timeout",
                "120",
            ],
            "credentials.json",
        )
        if credentials != []:
            manifest["release_blockers"].append(
                {"scan": "credentials", "reason": "findings_or_incomplete"}
            )
        skips = [
            argument
            for path in (
                ".venv",
                "node_modules",
                ".cache",
                ".tools",
                ".git",
                "docs/10-testing",
                "evals/reports",
            )
            for argument in ("--skip-dirs", path)
        ]
        sbom = run(
            "source-sbom",
            [
                trivy,
                "fs",
                "--cache-backend",
                "memory",
                "--quiet",
                "--scanners",
                "vuln",
                "--include-dev-deps",
                *skips,
                "--format",
                "cyclonedx",
                "--output",
                str(REPORTS / "source-sbom.cdx.json"),
                "--timeout",
                "5m",
                ".",
            ],
            "source-sbom.cdx.json",
        )
        if not isinstance(sbom, dict) or not sbom.get("components") or sbom.get("vulnerabilities"):
            manifest["release_blockers"].append(
                {"scan": "source-sbom", "reason": "findings_or_incomplete"}
            )
        images = json.loads((ROOT / "infra/images.lock").read_text())
        images.update(
            {
                key: value["image"]
                for key, value in json.loads(
                    (ROOT / "infra/monitoring/images.lock.json").read_text()
                ).items()
            }
        )
        images["parser"] = json.loads((ROOT / "infra/parser/image.lock.json").read_text())[
            "image_id"
        ]
        images["parser-service"] = parser_service_image
        images.update({"app-" + name: value for name, value in verified_images(ROOT).items()})
        manifest["inputs"]["infra/application/build.lock.json"] = digest(
            ROOT / "infra/application/build.lock.json"
        )
        manifest["inputs"].update(
            json.loads((ROOT / "infra/application/build.lock.json").read_text())["inputs_sha256"]
        )
        for name, image in images.items():
            output = f"image-{name}.json"
            data = run(
                "image-" + name,
                [
                    trivy,
                    "image",
                    "--cache-backend",
                    "memory",
                    "--quiet",
                    "--image-src",
                    "docker",
                    "--scanners",
                    "vuln",
                    "--format",
                    "json",
                    "--output",
                    str(REPORTS / output),
                    "--timeout",
                    "5m",
                    image,
                ],
                output,
            )
            if isinstance(data, dict):
                if not data.get("Results"):
                    manifest["release_blockers"].append(
                        {"scan": name, "reason": "empty_image_analysis"}
                    )
                manifest["release_blockers"].extend(
                    {"scan": name, **finding} for finding in blockers(data)
                )
            save()
        try:
            parser_service_unchanged = verified_parser_service(ROOT) == (
                parser_service_image,
                parser_service_inputs,
            )
        except ValueError, OSError:
            parser_service_unchanged = False
        if not parser_service_unchanged or any(
            digest(ROOT / name) != value for name, value in manifest["inputs"].items()
        ):
            manifest["release_blockers"].append(
                {"scan": "inputs", "reason": "changed_during_audit"}
            )
        manifest["status"] = "blocked" if manifest["release_blockers"] else "passed"
    except Exception:
        manifest["status"] = "incomplete"
        manifest["release_blockers"].append(
            {"scan": "runner", "reason": "unexpected_failure_no_release_approval"}
        )
        raise
    finally:
        manifest["finished_at_unix"] = time.time()
        save()
    print(
        f"Supply-chain gate: {manifest['status']}; {len(manifest['release_blockers'])} unresolved entries"
    )
    raise SystemExit(0 if manifest["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
