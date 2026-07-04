"""Real network-none containers, real kernel restrictions and actual format extraction."""

import base64
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from evidencedesk.ingestion.parser_protocol import service_fingerprint
from evidencedesk.ingestion.validation import MIMES

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def parser_service():
    lock = json.loads((ROOT / "infra/parser/service-build.lock.json").read_text())
    assert (
        service_fingerprint(ROOT / "infra/parser/service.lock.json", ROOT) == lock["source_sha256"]
    ), "Rebuild parser service after source changes"
    name, volume = (
        "ed-parser-service-test-" + uuid4().hex[:12],
        "ed-parser-socket-test-" + uuid4().hex[:12],
    )
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "--network=none",
                "--ipc=none",
                "--read-only",
                "--user=0:0",
                "--cap-drop=ALL",
                "--cap-add=SETUID",
                "--cap-add=SETGID",
                "--security-opt=no-new-privileges",
                "--pids-limit=32",
                "--memory=1280m",
                "--memory-swap=1280m",
                "--cpus=1",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=335544320,mode=0700",
                "--tmpfs",
                "/client:rw,noexec,nosuid,nodev,size=33554432,mode=0700,uid=10001,gid=10001",
                "--mount",
                f"type=volume,source={volume},target=/run/parser",
                lock["image_id"],
            ],
            check=True,
            capture_output=True,
        )
        for _ in range(50):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-m",
                    "evidencedesk.ingestion.parser_service",
                    "--health",
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(subprocess.check_output(["docker", "logs", name], text=True))
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


CLIENT = """
import asyncio, base64, hashlib, json, sys
from pathlib import Path
from evidencedesk.errors import Problem
from evidencedesk.ingestion.parser_client import SocketParserSandbox
request = json.load(sys.stdin)
source = Path('/client/input')
source.write_bytes(base64.b64decode(request['data']))
client = SocketParserSandbox(Path('/run/parser/parser.sock'), Path('infra/parser/service.lock.json'), seconds=request.get('seconds', 100))
async def run():
    try:
        result = await client.parse(source, request['title'], request['mime'], request.get('sha256', hashlib.sha256(source.read_bytes()).hexdigest()), Path('/client/output'))
        print(json.dumps({'blocks': [b.text for b in result.blocks]}))
    except Problem as exc:
        print(json.dumps({'error_code': exc.code}))
asyncio.run(run())
"""


def client(name, content, title="doc.txt", mime="text/plain", **kwargs):
    request = {"data": base64.b64encode(content).decode(), "title": title, "mime": mime, **kwargs}
    result = subprocess.run(
        ["docker", "exec", "-i", "--user=10001:10001", name, "python", "-c", CLIENT],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=110,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_ten_golden_documents_through_networkless_service(parser_service, golden_sources):
    for source, expected in golden_sources:
        assert client(parser_service, source.read_bytes(), source.name, MIMES[source.suffix]) == {
            "blocks": expected
        }


def test_container_has_no_host_socket_credentials_or_network(parser_service):
    config = json.loads(subprocess.check_output(["docker", "inspect", parser_service], text=True))[
        0
    ]
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
    assert host["IpcMode"] == "none"
    assert host["PidsLimit"] == 32
    assert host["Memory"] == host["MemorySwap"] == 1280 * 1024**2
    assert host["NanoCpus"] == 10**9
    assert {cap.removeprefix("CAP_") for cap in host["CapAdd"]} == {"SETUID", "SETGID"}
    assert host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
    assert all(m["Destination"] == "/run/parser" for m in config["Mounts"] if m["Type"] == "volume")
    assert not any(m["Type"] == "bind" for m in config["Mounts"])
    assert not any(value.startswith("ED_") for value in config["Config"]["Env"])


def test_real_child_uid_seccomp_filesystem_and_process_boundaries(parser_service):
    probe = """
import ctypes, errno, json, os, resource, socket, subprocess
from pathlib import Path
from evidencedesk.ingestion.parser_isolation import isolate
Path('/tmp/supervisor-private').write_text('private request bytes')
isolate()
result = {'uid': os.getuid(), 'gid': os.getgid(), 'groups': os.getgroups()}
try: Path('/tmp/forbidden-write').write_text('must never persist')
except OSError as exc: result['write'] = exc.errno
libc = ctypes.CDLL(None, use_errno=True)
result['shmget'] = libc.shmget(0, 4096, 0o1000 | 0o600)
result['shmget_errno'] = ctypes.get_errno()
for family, key in [(socket.AF_INET, 'inet'), (socket.AF_UNIX, 'unix')]:
    try: socket.socket(family)
    except OSError as exc: result[key] = exc.errno
try: subprocess.run(['/bin/true'], check=True)
except OSError as exc: result['exec'] = exc.errno
for path, key in [('/tmp/supervisor-private', 'input'), ('/client/input', 'peer'), ('/proc/1/mem', 'supervisor')]:
    try: Path(path).read_bytes()
    except OSError as exc: result[key] = exc.errno
status = Path('/proc/self/status').read_text().splitlines()
result['status'] = {line.split(':')[0]: line.split(':')[1].strip() for line in status if line.split(':')[0] in {'CapEff', 'CapPrm', 'NoNewPrivs', 'Seccomp'}}
result['cpu'] = resource.getrlimit(resource.RLIMIT_CPU)
result['memory'] = resource.getrlimit(resource.RLIMIT_AS)
print(json.dumps(result))
"""
    result = subprocess.run(
        ["docker", "exec", parser_service, "python", "-c", probe],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["uid"] == report["gid"] == 65532 and report["groups"] == []
    assert report["inet"] == report["unix"] == report["exec"] == 1  # EPERM
    assert report["write"] == report["shmget_errno"] == 1 and report["shmget"] == -1
    assert report["input"] == report["peer"] == report["supervisor"] == 13  # EACCES
    assert report["status"] == {
        "CapEff": "0000000000000000",
        "CapPrm": "0000000000000000",
        "NoNewPrivs": "1",
        "Seccomp": "2",
    }
    assert report["cpu"] == [60, 60] and report["memory"] == [1024**3, 1024**3]


def test_hash_failure_and_cancel_do_not_leave_a_child_or_poison_service(parser_service):
    assert client(parser_service, b"A private fixture.", sha256="0" * 64) == {
        "error_code": "object_checksum_mismatch"
    }
    cancellation = """
import asyncio, hashlib, json
from contextlib import suppress
from pathlib import Path
from evidencedesk.ingestion.parser_client import SocketParserSandbox
source = Path('/client/input')
source.write_bytes(b'a' * (19 * 1024**2))
client = SocketParserSandbox(Path('/run/parser/parser.sock'), Path('infra/parser/service.lock.json'))
async def run():
    task = asyncio.create_task(client.parse(source, 'slow.txt', 'text/plain', hashlib.sha256(source.read_bytes()).hexdigest(), Path('/client/output')))
    observed = False
    try:
        async with asyncio.timeout(10):
            while not task.done() and not observed:
                for path in Path('/proc').iterdir():
                    if not path.name.isdigit(): continue
                    with suppress(OSError):
                        command = (path / 'cmdline').read_bytes().split(b'\\0')
                        if command[1:3] == [b'-m', b'evidencedesk.ingestion.parser_service_child']:
                            uid = next(line for line in (path / 'status').read_text().splitlines() if line.startswith('Uid:'))
                            observed = uid.split()[1:] == ['65532'] * 4
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError): await task
    print(json.dumps({'observed_isolated_child': observed}))
asyncio.run(run())
"""
    result = subprocess.run(
        ["docker", "exec", "--user=10001:10001", parser_service, "python", "-c", cancellation],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"observed_isolated_child": True}
    # The next extraction also proves the single supervisor got past disconnect cleanup.
    assert client(parser_service, b"Next document.") == {"blocks": ["Next document."]}
    processes = subprocess.check_output(
        ["docker", "top", parser_service, "-eo", "pid,args"], text=True
    )
    assert "parser_service_child" not in processes


def test_rejected_header_and_fingerprint_do_not_start_parse(parser_service):
    probe = """
import json, socket, struct
from evidencedesk.ingestion.parser_protocol import service_fingerprint
from pathlib import Path
fingerprint = service_fingerprint(Path('infra/parser/service.lock.json'))
results = []
for raw in [b'[' * 1500 + b']' * 1500, b'{"version":1,"version":1}', json.dumps({'version':1,'operation':'parse','fingerprint':'f'*64,'title':'x.txt','mime':'text/plain','sha256':'a'*64,'size':1}).encode(), json.dumps({'version':1,'operation':'health','fingerprint':'f'*64}).encode()]:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect('/run/parser/parser.sock')
        connection.sendall(struct.pack('!I', len(raw)) + raw)
        size = struct.unpack('!I', connection.recv(4))[0]
        results.append(json.loads(connection.recv(size))['error_code'])
print(json.dumps(results))
"""
    result = subprocess.run(
        ["docker", "exec", "--user=10001:10001", parser_service, "python", "-c", probe],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["parser_protocol_failure"] * 4
