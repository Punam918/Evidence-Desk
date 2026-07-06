import hashlib
import json

import pytest

from scripts import security_audit


def test_every_high_or_critical_image_occurrence_blocks_release():
    findings = [
        {
            "VulnerabilityID": "fixture-" + severity,
            "Severity": severity,
            "PkgName": "library",
            "InstalledVersion": "1",
            "FixedVersion": "2",
        }
        for severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    ]
    result = security_audit.blockers(
        {
            "Results": [
                {"Target": "server", "Vulnerabilities": findings},
                {"Target": "helper", "Vulnerabilities": findings},
            ]
        }
    )
    assert len(result) == 4
    assert {row["target"] for row in result} == {"server", "helper"}
    assert {row["severity"] for row in result} == {"HIGH", "CRITICAL"}


def test_modified_scanner_binary_is_never_executed(tmp_path, monkeypatch):
    tools = tmp_path / ".tools/security"
    tools.mkdir(parents=True)
    tools.joinpath("trivy").write_bytes(b"tampered")
    lock = tmp_path / "infra/security/tools.lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({"trivy": {"binary_sha256": hashlib.sha256(b"approved").hexdigest()}})
    )
    monkeypatch.setattr(security_audit, "ROOT", tmp_path)
    monkeypatch.setattr(security_audit, "TOOLS", tools)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        security_audit.scanner("trivy", bootstrap=False)
