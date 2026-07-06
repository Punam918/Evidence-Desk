"""Persistent credentials are isolated by mounted profile, not by configuration promises."""

import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from infra.bootstrap.entrypoint import PROFILE_ROLES, ROLE_VARIABLES, runtime_environment
from infra.bootstrap.secrets import (
    PROFILES,
    SECRET_LENGTHS,
    initialize,
    read_private,
    write_private,
)


def prepare(root: Path) -> None:
    initialize(root, uid=os.getuid(), gid=os.getgid(), profile_owners={})


def test_secret_profiles_persist_without_rotation_and_are_private(tmp_path):
    prepare(tmp_path)
    initial = {name: read_private(tmp_path / "operator" / name) for name in SECRET_LENGTHS}
    assert len(set(initial.values())) == len(initial)
    prepare(tmp_path)
    assert initial == {name: read_private(tmp_path / "operator" / name) for name in SECRET_LENGTHS}
    for profile, names in PROFILES.items():
        directory = tmp_path / profile
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert {path.name for path in directory.iterdir()} == set(names)
        for name in names:
            assert read_private(directory / name) == initial[name]
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert "postgres_password" not in PROFILES["api"]
    assert "oidc_client_secret" not in PROFILES["chat"]
    assert "qdrant_write_key" not in PROFILES["chat"]
    assert "s3_secret_key" not in PROFILES["chat"]


def test_changed_profile_is_rejected_without_rotation(tmp_path):
    prepare(tmp_path)
    original = read_private(tmp_path / "operator" / "ed_app_password")
    (tmp_path / "api" / "ed_app_password").write_bytes(b"1" * 64 + b"\n")
    with pytest.raises(ValueError, match="rotation is forbidden"):
        prepare(tmp_path)
    assert read_private(tmp_path / "operator" / "ed_app_password") == original


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "world_readable", "directory"])
def test_secret_file_boundary_rejects_shared_or_indirect_paths(tmp_path, kind):
    source = tmp_path / "source"
    write_private(source, b"test", uid=os.getuid(), gid=os.getgid())
    target = tmp_path / "target"
    if kind == "symlink":
        target.symlink_to(source)
    elif kind == "hardlink":
        target.hardlink_to(source)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"test")
        target.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        read_private(target)


def test_symlink_profile_rejected_without_writing_external_directory(tmp_path):
    destination = tmp_path / "outside"
    destination.mkdir()
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "operator").symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        prepare(root)
    assert not list(destination.iterdir())


def test_bootstrap_extension_files_retained_and_changed_only_explicitly(tmp_path):
    prepare(tmp_path)
    config = tmp_path / "identity" / "realm.json"
    options = {"uid": os.getuid(), "gid": os.getgid()}
    assert write_private(config, b"one", **options)
    assert not write_private(config, b"one", **options)
    prepare(tmp_path)
    assert read_private(config) == b"one"
    with pytest.raises(ValueError):
        write_private(config, b"two", **options)
    assert write_private(config, b"two", replace=True, **options)


@pytest.mark.parametrize("profile", tuple(PROFILE_ROLES))
def test_runtime_environment_contains_only_profile_database_roles(tmp_path, profile):
    prepare(tmp_path)
    inherited = {
        name: "postgresql://postgres:forbidden@postgres/postgres"
        for name in ROLE_VARIABLES.values()
    }
    inherited["ED_MIGRATION_DATABASE_URL"] = "postgresql://postgres:forbidden@postgres/postgres"
    inherited["ED_DB_HOST"] = "postgres"
    result = runtime_environment(profile, tmp_path / profile, inherited)
    assert "ED_MIGRATION_DATABASE_URL" not in result
    expected = {ROLE_VARIABLES[role] for role in PROFILE_ROLES[profile]}
    assert set(result) & set(ROLE_VARIABLES.values()) == expected
    for role in PROFILE_ROLES[profile]:
        url = urlsplit(result[ROLE_VARIABLES[role]])
        assert url.username == role
        assert url.hostname == "postgres"
        assert url.path == "/evidencedesk"
        assert (
            url.password == read_private(tmp_path / profile / f"{role}_password").decode().strip()
        )


@pytest.mark.parametrize(
    "host", ["user@postgres", "postgres/other", "postgres?options=bad", "postgres:5432"]
)
def test_database_host_cannot_inject_credentials_or_url_options(tmp_path, host):
    prepare(tmp_path)
    with pytest.raises(ValueError, match="database address"):
        runtime_environment("api", tmp_path / "api", {"ED_DB_HOST": host})


@pytest.mark.integration
def test_docker_subpath_mount_reads_postgres_profile_with_private_volume_parent():
    """Production UID999 can read its subpath even though volume root is UID10001/0700."""
    root = Path(__file__).resolve().parents[2]
    volume = "ed-bootstrap-secret-test-" + uuid4().hex[:12]
    python_image = (
        "python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
    )
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--read-only",
                "--network=none",
                "--cap-drop=ALL",
                "--cap-add=CHOWN",
                "--cap-add=FOWNER",
                "--cap-add=DAC_OVERRIDE",
                "--security-opt=no-new-privileges",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--mount",
                f"type=volume,src={volume},dst=/secrets",
                "--mount",
                f"type=bind,src={root / 'infra/bootstrap'},dst=/app/infra/bootstrap,readonly",
                "--workdir",
                "/app",
                python_image,
                "python",
                "-m",
                "infra.bootstrap.secrets",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        script = (
            "from pathlib import Path; import os, stat; "
            "p=Path('/run/secrets'); assert os.geteuid()==999; "
            "assert {f.name for f in p.iterdir()}=={'postgres_password'}; "
            "f=p/'postgres_password'; assert f.stat().st_uid==999; "
            "assert stat.S_IMODE(f.stat().st_mode)==0o600; "
            "assert len(f.read_bytes().strip())==64"
        )
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "999:999",
                "--read-only",
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--mount",
                f"type=volume,src={volume},dst=/run/secrets,volume-subpath=postgres,readonly",
                python_image,
                "python",
                "-c",
                script,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True, capture_output=True)
