import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from evidencedesk.errors import Problem
from evidencedesk.ingestion.sandbox import ParserSandbox

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def parser_image():
    lock = json.loads((ROOT / "infra/parser/image.lock.json").read_text())
    for name, digest in lock["files"].items():
        path = ROOT / (
            "apps/api/src/" + name if name.startswith("evidencedesk/") else "infra/parser/" + name
        )
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
            "Rebuild parser image after source changes"
        )
    return lock["image_id"]


async def test_real_isolated_parser_and_identity(parser_image, tmp_path):
    source = tmp_path / "source"
    source.write_text("# Policy\n\nAlways cite the original evidence.")
    source.chmod(0o444)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = await ParserSandbox(parser_image).parse(
        source, "policy.md", "text/markdown", digest, tmp_path / "blocks.json"
    )
    assert [b.text for b in result.blocks] == ["# Policy", "Always cite the original evidence."]
    assert result.original_sha256 == digest
    with pytest.raises(Problem) as exc:
        await ParserSandbox(parser_image).parse(
            source, "policy.md", "text/markdown", "f" * 64, tmp_path / "bad.json"
        )
    assert exc.value.code == "object_checksum_mismatch"


async def test_container_constraints_and_cancel_cleanup(parser_image, tmp_path):
    # A large valid document keeps extraction alive long enough to inspect the actual process.
    source = tmp_path / "source"
    source.write_text("a" * (19 * 1024 * 1024))
    source.chmod(0o444)
    before = set(
        subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "name=ed-parser-"], text=True
        ).split()
    )
    task = asyncio.create_task(
        ParserSandbox(parser_image).parse(
            source,
            "large.txt",
            "text/plain",
            hashlib.sha256(source.read_bytes()).hexdigest(),
            tmp_path / "out",
        )
    )
    owned = None
    try:
        for _ in range(150):
            result = await asyncio.create_subprocess_exec(
                "docker", "ps", "-aq", "--filter", "name=ed-parser-", stdout=asyncio.subprocess.PIPE
            )
            raw, _ = await result.communicate()
            created = set(raw.decode().split()) - before
            if created:
                owned = next(iter(created))
                break
            await asyncio.sleep(0.02)
        assert owned
        config = json.loads(subprocess.check_output(["docker", "inspect", owned], text=True))[0]
        host = config["HostConfig"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
        assert host["Memory"] == host["MemorySwap"] == 1024**3 and host["NanoCpus"] == 10**9
        assert config["Config"]["User"] == "65532:65532"
        assert "ALL" in host["CapDrop"] and "no-new-privileges" in host["SecurityOpt"]
        assert host["PidsLimit"] == 32
        assert all(
            m["Destination"] == "/input/source" and not m["RW"]
            for m in config["Mounts"]
            if m["Type"] == "bind"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert subprocess.run(["docker", "inspect", owned], capture_output=True).returncode != 0


async def test_parser_deadline_fails_closed(parser_image, tmp_path):
    source = tmp_path / "source"
    source.write_text("a" * (19 * 1024 * 1024))
    source.chmod(0o444)
    with pytest.raises(Problem) as exc:
        await ParserSandbox(parser_image, seconds=0.5).parse(
            source,
            "slow.txt",
            "text/plain",
            hashlib.sha256(source.read_bytes()).hexdigest(),
            tmp_path / "out",
        )
    assert exc.value.code == "parser_timeout"
