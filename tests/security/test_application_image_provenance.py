import hashlib
import json

import pytest

from scripts import application_images


@pytest.fixture
def source_tree(tmp_path):
    for directory in application_images.DIRECTORIES:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for name in application_images.FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic build input")
    return tmp_path


def record(root):
    path = root / application_images.MANIFEST
    data = {
        "inputs_sha256": application_images.inputs(root),
        "images": {
            "application": "sha256:" + "a" * 64,
            "web": "sha256:" + "b" * 64,
            "identity": "sha256:" + "c" * 64,
        },
    }
    path.write_text(json.dumps(data))
    return path, data


@pytest.mark.parametrize(
    "changed",
    [
        "apps/api/src/policy.py",
        "apps/web/src/style.css",
        "contracts/config/models.yaml",
        "infra/application/Caddyfile",
    ],
)
def test_changed_runtime_source_rejects_prior_image_identity(source_tree, changed):
    record(source_tree)
    (source_tree / changed).write_text("changed behavior")
    with pytest.raises(ValueError, match="inputs changed"):
        application_images.verified_images(source_tree)


def test_only_exact_built_image_ids_can_enter_the_scan(source_tree):
    path, data = record(source_tree)
    assert len(application_images.verified_images(source_tree)) == 3
    data["images"]["application"] = "evidencedesk-application:local"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exact image IDs"):
        application_images.verified_images(source_tree)
    path.unlink()
    with pytest.raises(ValueError, match="manifest missing"):
        application_images.verified_images(source_tree)


def test_secret_and_cache_paths_are_not_read_or_fingerprinted(source_tree):
    secret = source_tree / "apps/api/src/.env"
    secret.write_text("TEST-ONLY-SHOULD-NOT-BE-READ")
    cached = source_tree / "apps/api/src/__pycache__"
    cached.mkdir()
    (cached / "test.pyc").write_text("cache")
    values = application_images.inputs(source_tree)
    assert not any(".env" in name or "__pycache__" in name for name in values)
    assert hashlib.sha256(secret.read_bytes()).hexdigest() not in values.values()


def test_symlinked_runtime_source_is_not_a_build_identity(source_tree, tmp_path):
    (source_tree / "apps/api/src/escape.py").symlink_to(tmp_path / "uv.lock")
    with pytest.raises(ValueError, match="Symbolic build"):
        application_images.inputs(source_tree)
