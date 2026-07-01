"""Download locked model artifacts into fresh disposable volumes, without generation.

Runs the exact application image initializer twice against isolated, bounded services.
Never mounts host model caches or accesses an existing Ollama service. All resources
created by this validator are removed on exit; reports contain no remote log bodies.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3
OLLAMA_IMAGE = (
    "ollama/ollama@sha256:adca95a12c2b386a8f9a995e7fa0600e187e3323b5e5522f5a6c3b1ec73eff99"
)
INPUTS = (
    "infra/bootstrap/models.py",
    "scripts/prepare_models.py",
    "contracts/config/model-artifacts.lock.json",
    "contracts/config/generation-tokenizer.lock.json",
    "contracts/config/local-tokenizer.lock.json",
    "contracts/config/local-runtime.lock.json",
)


def docker(*arguments: str, timeout: int = 30, source: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        input=source,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        # Registry/HF errors can contain signed download URLs; do not serialize raw stderr.
        raise RuntimeError("Docker operation failed")
    return result.stdout.strip()


def available_memory() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Host available memory could not be measured")


def image_id(reference: str) -> str:
    value = docker("image", "inspect", reference, "--format", "{{.Id}}")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("Unexpected local image identity")
    return value


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name))[0]


def no_generation(name: str) -> None:
    # ollama ps is metadata-only and must have only its header before/after downloads.
    output = docker("exec", name, "ollama", "ps")
    if len(output.splitlines()) != 1:
        raise ValueError("Cold download unexpectedly loaded a generation model")


def verified_limits(name: str, memory: int) -> dict:
    configured = inspect(name)["HostConfig"]
    observed = {
        "memory_bytes": configured["Memory"],
        "memory_plus_swap_bytes": configured["MemorySwap"],
        "nano_cpus": configured["NanoCpus"],
    }
    if observed != {
        "memory_bytes": memory,
        "memory_plus_swap_bytes": memory,
        "nano_cpus": 1_000_000_000,
    }:
        raise ValueError("Container resource limits differ from the approved cold-test bounds")
    if configured.get("PortBindings"):
        raise ValueError("Cold-test service unexpectedly publishes a host port")
    return observed


def run(args: argparse.Namespace) -> dict:
    prefix = "ed-coldbootstrap-" + uuid4().hex[:12]
    network = prefix + "-net"
    model_volume, ollama_volume = prefix + "-models", prefix + "-ollama"
    ollama = prefix + "-ollama"
    report = {
        "version": 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "passed": False,
        "generator_requests": 0,
        "host_caches_mounted": False,
        "existing_services_accessed": False,
        "published_ports": [],
        "limits": {
            "ollama_memory_bytes": 512 * 1024**2,
            "bootstrap_memory_bytes": int(1.5 * GIB),
            "cpus_each": 1,
            "swap_enabled": False,
        },
        "preflight": {
            "free_disk_bytes": shutil.disk_usage(ROOT).free,
            "available_memory_bytes": available_memory(),
        },
        "runs": [],
        "resource_prefix": prefix,
    }
    containers, volumes, networks = [], [], []
    stage = "preflight"
    total_started = time.monotonic()
    try:
        if report["preflight"]["free_disk_bytes"] < 20 * GIB:
            raise ValueError("Cold bootstrap requires at least20GiB free disk")
        if report["preflight"]["available_memory_bytes"] < 5 * GIB:
            raise ValueError("Cold bootstrap requires at least5GiB available memory")
        application = image_id(args.image)
        report["images"] = {
            "application": application,
            "ollama": image_id(OLLAMA_IMAGE),
            "ollama_reference": OLLAMA_IMAGE,
        }
        hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS}
        stage = "verify_image_initializer_sources"
        verification = (
            "import hashlib,json; from pathlib import Path; "
            f"paths={list(INPUTS)!r}; "
            "print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}))"
        )
        helper = prefix + "-provenance"
        containers.append(helper)
        observed = json.loads(
            docker(
                "run",
                "--rm",
                "--name",
                helper,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=128m",
                "--memory-swap=128m",
                "--cpus=1",
                "--no-healthcheck",
                "--entrypoint=python",
                application,
                "-c",
                verification,
            )
        )
        if observed != hashes:
            raise ValueError(
                "Application image bootstrap/config sources differ; rebuild before cold validation"
            )
        report["inputs_sha256"] = hashes
        docker("network", "create", network)
        networks.append(network)
        for volume in (model_volume, ollama_volume):
            docker("volume", "create", volume)
            volumes.append(volume)
        stage = "verify_empty_volumes"
        helper = prefix + "-permissions"
        containers.append(helper)
        empty = (
            "from pathlib import Path; import os; "
            "paths=[Path('/models'),Path('/ollama')]; "
            "assert all(not any(p.iterdir()) for p in paths); "
            "os.chown('/models',10001,10001); os.chmod('/models',0o700)"
        )
        docker(
            "run",
            "--rm",
            "--name",
            helper,
            "--network=none",
            "--user=0:0",
            "--read-only",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--security-opt=no-new-privileges",
            "--memory=128m",
            "--memory-swap=128m",
            "--cpus=1",
            "--no-healthcheck",
            "--mount",
            f"type=volume,src={model_volume},dst=/models",
            "--mount",
            f"type=volume,src={ollama_volume},dst=/ollama",
            "--entrypoint=python",
            application,
            "-c",
            empty,
        )
        report["volumes_verified_empty_before_start"] = True
        stage = "start_isolated_ollama"
        containers.append(ollama)
        docker(
            "run",
            "-d",
            "--name",
            ollama,
            "--network",
            network,
            "--network-alias=ollama",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=1",
            "--pids-limit=128",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--env=OLLAMA_NO_CLOUD=1",
            "--env=OLLAMA_NUM_PARALLEL=1",
            "--env=OLLAMA_MAX_LOADED_MODELS=1",
            "--mount",
            f"type=volume,src={ollama_volume},dst=/root/.ollama",
            OLLAMA_IMAGE,
        )
        report["applied_ollama_limits"] = verified_limits(ollama, 512 * 1024**2)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", ollama, "ollama", "list"], capture_output=True, timeout=10
            )
            if ready.returncode == 0:
                break
            if not inspect(ollama)["State"]["Running"]:
                raise RuntimeError("Isolated Ollama exited before readiness")
            time.sleep(1)
        else:
            raise TimeoutError("Isolated Ollama readiness timed out")
        no_generation(ollama)
        for index in (1, 2):
            stage = f"bootstrap_run_{index}"
            name = prefix + f"-run-{index}"
            containers.append(name)
            start = time.monotonic()
            print(f"Cold-bootstrap verification: starting pass{index}", flush=True)
            # The second invocation must work with downloads disabled, rather
            # than silently repairing a missing/corrupt artifact from the net.
            download_arguments = ["--download"] if index == 1 else []
            offline = "0" if index == 1 else "1"
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--user=10001:10001",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=1536m",
                "--memory-swap=1536m",
                "--cpus=1",
                "--no-healthcheck",
                "--tmpfs=/tmp:uid=10001,gid=10001,mode=0700,size=96m",
                f"--env=HF_HUB_OFFLINE={offline}",
                f"--env=TRANSFORMERS_OFFLINE={offline}",
                "--env=HF_HUB_DISABLE_TELEMETRY=1",
                "--env=HF_HUB_DISABLE_PROGRESS_BARS=1",
                "--env=ED_MODEL_CACHE_PATH=/models",
                "--mount",
                f"type=volume,src={model_volume},dst=/models",
                "--entrypoint=python",
                application,
                "-m",
                "infra.bootstrap.models",
                *download_arguments,
                "--ollama-url=http://ollama:11434",
                f"--timeout-seconds={args.timeout_seconds}",
            )
            applied_limits = verified_limits(name, int(1.5 * GIB))
            next_progress = start + 30
            while True:
                metadata = inspect(name)
                if not metadata["State"]["Running"]:
                    break
                if time.monotonic() - start > args.timeout_seconds * 2 + 60:
                    raise TimeoutError("Cold bootstrap exceeded its overall limit")
                if time.monotonic() >= next_progress:
                    print(
                        f"Cold-bootstrap verification: pass{index} still running ({int(time.monotonic() - start)}s)",
                        flush=True,
                    )
                    next_progress += 30
                time.sleep(2)
            outcome = {
                "pass": index,
                "downloads_allowed": index == 1,
                "duration_seconds": round(time.monotonic() - start, 3),
                "exit_code": metadata["State"]["ExitCode"],
                "oom_killed": metadata["State"]["OOMKilled"],
                "applied_limits": applied_limits,
            }
            log_result = subprocess.run(
                ["docker", "logs", "--tail", "100", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logs = log_result.stdout + "\n" + log_result.stderr
            outcome["verified_stages"] = [
                line
                for line in logs.splitlines()
                if re.fullmatch(
                    r"(?:embedding|reranker|sparse): immutable snapshot [a-f0-9]{40} verified|generation tokenizer: o200k_base immutable artifact verified|local generation tokenizer: immutable Qwen snapshot verified|local generation: evidencedesk-qwen3-8b:[a-f0-9]+ exact digest and metadata verified",
                    line,
                )
            ]
            report["runs"].append(outcome)
            if outcome["exit_code"] != 0 or outcome["oom_killed"]:
                outcome["diagnostic_classes"] = sorted(
                    set(
                        re.findall(
                            r"\b(PermissionError|FileNotFoundError|OSError|RuntimeError|ValueError|LocalEntryNotFoundError|ReadTimeout|ConnectTimeout|HTTPStatusError|CalledProcessError|ConnectError|HfHubHTTPError|RepositoryNotFoundError|RevisionNotFoundError|EntryNotFoundError)\b",
                            logs,
                        )
                    )
                )
                outcome["diagnostic_errno"] = sorted(set(re.findall(r"\[Errno (\d+)\]", logs)))
                outcome["diagnostic_flags"] = {
                    "read_only_filesystem": "Read-only file system" in logs,
                    "permission_denied": "Permission denied" in logs,
                    "xet_mentioned": "xet" in logs.lower(),
                    "certificate_verification_failed": "CERTIFICATE_VERIFY_FAILED" in logs,
                    "disk_full": "No space left on device" in logs,
                }
                raise RuntimeError("Cold bootstrap process failed; raw download logs withheld")
            if len(outcome["verified_stages"]) != 6:
                raise RuntimeError("Cold bootstrap did not verify every expected artifact family")
            no_generation(ollama)
        stage = "record_downloaded_artifact_checksums"
        helper = prefix + "-artifacts"
        containers.append(helper)
        inventory = """import hashlib,json
from pathlib import Path
cache=Path('/models'); results=[]
for record in json.loads(Path('contracts/config/model-artifacts.lock.json').read_text())['models']:
    base=cache/('fastembed' if record['kind']=='sparse' else '')/('models--'+record['model_id'].replace('/','--'))/'snapshots'/record['revision']
    for name,expected in record['files_sha256'].items():
        file=base/name
        with file.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
        assert actual==expected
        results.append({'family':record['kind'],'path':str(file.relative_to(cache)),'sha256':actual,'bytes':file.stat().st_size})
record=json.loads(Path('contracts/config/generation-tokenizer.lock.json').read_text())
file=cache/'tiktoken/o200k_base.tiktoken'
with file.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
assert actual==record['sha256']; results.append({'family':'generation_tokenizer','path':str(file.relative_to(cache)),'sha256':actual,'bytes':file.stat().st_size})
record=json.loads(Path('contracts/config/local-tokenizer.lock.json').read_text())
for name,expected in record['files_sha256'].items():
    file=cache/'qwen3-tokenizer'/name
    with file.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
    assert actual==expected
    results.append({'family':'local_tokenizer','path':str(file.relative_to(cache)),'sha256':actual,'bytes':file.stat().st_size})
print(json.dumps(results))
"""
        report["downloaded_files"] = json.loads(
            docker(
                "run",
                "--rm",
                "--name",
                helper,
                "--network=none",
                "--user=10001:10001",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=128m",
                "--memory-swap=128m",
                "--cpus=1",
                "--no-healthcheck",
                "--mount",
                f"type=volume,src={model_volume},dst=/models,readonly",
                "--entrypoint=python",
                application,
                "-c",
                inventory,
                timeout=120,
            )
        )
        report["local_generation_lock"] = json.loads(
            (ROOT / "contracts/config/local-runtime.lock.json").read_text()
        )
        stage = "verify_downloaded_ollama_blobs"
        helper = prefix + "-ollama-artifacts"
        containers.append(helper)
        ollama_inventory = """import hashlib,json,re
from pathlib import Path
base=Path('/ollama/models'); lock=json.loads(Path('contracts/config/local-runtime.lock.json').read_text()); result=[]
for file in sorted((base/'blobs').glob('sha256-*')):
    assert file.is_file() and not file.is_symlink() and re.fullmatch('sha256-[a-f0-9]{64}',file.name)
    with file.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
    assert actual==file.name.removeprefix('sha256-')
    result.append({'path':str(file.relative_to(base)),'sha256':actual,'bytes':file.stat().st_size})
assert result
# `/api/tags` and `/api/show` verified the exact lock digest and metadata in
# each bootstrap pass. Ollama deliberately owns the on-disk alias-manifest
# layout, including after /api/copy, so only inventory regular manifests here.
manifests=[]
root=base/'manifests'
if root.exists():
    for file in sorted(root.rglob('*')):
        if file.is_file() and not file.is_symlink():
            with file.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
            manifests.append({'path':str(file.relative_to(base)),'sha256':actual,'bytes':file.stat().st_size})
print(json.dumps({'blobs':result,'manifests':manifests}))
"""
        report["downloaded_ollama_artifacts"] = json.loads(
            docker(
                "run",
                "--rm",
                "--name",
                helper,
                "--network=none",
                "--user=0:0",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=128m",
                "--memory-swap=128m",
                "--cpus=1",
                "--no-healthcheck",
                "--mount",
                f"type=volume,src={ollama_volume},dst=/ollama,readonly",
                "--entrypoint=python",
                application,
                "-c",
                ollama_inventory,
                timeout=180,
            )
        )
        stage = "verify_unchanged_bootstrap_sources"
        if hashes != {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS
        }:
            raise ValueError("Bootstrap inputs changed during cold validation")
        report["passed"] = True
    except Exception as error:
        report["failure"] = {"stage": stage, "kind": type(error).__name__}
    finally:
        failures = []
        for kind, names in (("container", containers), ("network", networks), ("volume", volumes)):
            for name in reversed(names):
                command = (
                    ["docker", "rm", "-f", name]
                    if kind == "container"
                    else ["docker", kind, "rm", name]
                )
                result = subprocess.run(command, capture_output=True, timeout=30)
                if result.returncode and kind != "container":
                    failures.append({"kind": kind, "name": name})
        report["cleanup"] = {"completed": not failures, "failures": failures}
        report["passed"] = report["passed"] and not failures
        report["total_duration_seconds"] = round(time.monotonic() - total_started, 3)
        report["postflight_free_disk_bytes"] = shutil.disk_usage(ROOT).free
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="evidencedesk-application:local")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new report path; evidence is not overwritten")
    if not 30 <= args.timeout_seconds <= 14400:
        parser.error("timeout-seconds must be between30 and14400")
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(
        "Cold model bootstrap "
        + ("passed" if result["passed"] else "failed")
        + "; see machine report."
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
