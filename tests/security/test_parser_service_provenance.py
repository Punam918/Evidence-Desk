import hashlib
import json
from pathlib import Path

import pytest

from scripts import security_audit


@pytest.fixture
def source_tree(tmp_path):
    files = {}
    for filename, value in {
        "evidencedesk/ingestion/parser_fixture.py": "reviewed parser behavior",
        "Dockerfile.service": "reviewed container build",
        "requirements.txt": "reviewed dependency hashes",
    }.items():
        relative = (
            Path("apps/api/src" if filename.startswith("evidencedesk/") else "infra/parser")
            / filename
        )
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        files[filename] = hashlib.sha256(value.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    (tmp_path / "infra/parser/service.lock.json").write_text(
        json.dumps({"protocol": 1, "source_sha256": fingerprint, "files": files})
    )
    (tmp_path / "infra/parser/service-build.lock.json").write_text(
        json.dumps({"image_id": "sha256:" + "a" * 64, "source_sha256": fingerprint})
    )
    return tmp_path


def test_scan_receives_exact_service_image_and_every_build_input(source_tree):
    image, inputs = security_audit.verified_parser_service(source_tree)
    assert image == "sha256:" + "a" * 64
    assert set(inputs) == {
        "infra/parser/service-build.lock.json",
        "infra/parser/service.lock.json",
        "infra/parser/Dockerfile.service",
        "infra/parser/requirements.txt",
        "apps/api/src/evidencedesk/ingestion/parser_fixture.py",
    }
    assert all(
        security_audit.digest(source_tree / name) == digest for name, digest in inputs.items()
    )


@pytest.mark.parametrize("value", ["evidencedesk-parser-service:local", "sha256:short", None])
def test_mutable_or_invalid_service_image_is_never_scanned(source_tree, value):
    path = source_tree / "infra/parser/service-build.lock.json"
    manifest = json.loads(path.read_text())
    manifest["image_id"] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="exact built image ID"):
        security_audit.verified_parser_service(source_tree)


def test_new_source_lock_cannot_relabel_a_prior_built_image(source_tree):
    source = source_tree / "apps/api/src/evidencedesk/ingestion/parser_fixture.py"
    source.write_text("different parser behavior")
    path = source_tree / "infra/parser/service.lock.json"
    lock = json.loads(path.read_text())
    lock["files"]["evidencedesk/ingestion/parser_fixture.py"] = security_audit.digest(source)
    lock["source_sha256"] = hashlib.sha256(
        json.dumps(lock["files"], sort_keys=True).encode()
    ).hexdigest()
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="rebuild before release"):
        security_audit.verified_parser_service(source_tree)


@pytest.mark.parametrize(
    "changed",
    [
        "apps/api/src/evidencedesk/ingestion/parser_fixture.py",
        "infra/parser/Dockerfile.service",
        "infra/parser/requirements.txt",
    ],
)
def test_drift_during_audit_fails_final_service_reverification(source_tree, changed):
    _, inputs = security_audit.verified_parser_service(source_tree)
    (source_tree / changed).write_text("changed while scans were running")
    assert security_audit.digest(source_tree / changed) != inputs[changed]
    with pytest.raises(ValueError, match="differ from their lock"):
        security_audit.verified_parser_service(source_tree)


def test_changed_image_lock_is_visible_in_final_input_hashes(source_tree):
    original = security_audit.verified_parser_service(source_tree)
    path = source_tree / "infra/parser/service-build.lock.json"
    build = json.loads(path.read_text())
    build["image_id"] = "sha256:" + "b" * 64
    path.write_text(json.dumps(build))
    changed = security_audit.verified_parser_service(source_tree)
    assert changed != original
    assert (
        changed[1]["infra/parser/service-build.lock.json"]
        != original[1]["infra/parser/service-build.lock.json"]
    )


def test_missing_service_build_or_symbolic_lock_cannot_authorize_scan(source_tree):
    path = source_tree / "infra/parser/service-build.lock.json"
    saved = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match="lock missing"):
        security_audit.verified_parser_service(source_tree)
    target = source_tree / "other-lock.json"
    target.write_bytes(saved)
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        security_audit.verified_parser_service(source_tree)
