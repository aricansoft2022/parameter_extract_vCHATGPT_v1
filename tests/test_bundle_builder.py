import json
from pathlib import Path

import pytest

import parameter_extract.bundle_builder as builder_module
from parameter_extract.bundle_builder import seal_research_bundle
from parameter_extract.bundle_cli import build_parser
from parameter_extract.manifest import manifest_fingerprint


def _contracts(tmp_path: Path):
    manifest = {
        "schema_version": 1,
        "kind": "parameter_extract.data_manifest",
        "source": "fixture",
        "files": {"candles": None, "funding": None},
        "candles": {"integrity_ok": True},
    }
    manifest["dataset_fingerprint_sha256"] = manifest_fingerprint(manifest)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    study_path = tmp_path / "study.json"
    search_path = tmp_path / "search.json"
    calibration_path = tmp_path / "calibration.json"
    for path in (study_path, search_path, calibration_path):
        path.write_text("{}", encoding="utf-8")
    return manifest_path, study_path, search_path, calibration_path, manifest


def _patch_fingerprints(monkeypatch, *, verification_error: Exception | None = None):
    monkeypatch.setattr(builder_module, "load_study_json", lambda path: object())
    monkeypatch.setattr(builder_module, "study_fingerprint", lambda spec: "1" * 64)
    monkeypatch.setattr(builder_module, "load_search_json", lambda path: object())
    monkeypatch.setattr(builder_module, "search_fingerprint", lambda spec: "2" * 64)
    monkeypatch.setattr(builder_module, "load_scale_calibration_json", lambda path: object())
    monkeypatch.setattr(builder_module, "scale_calibration_fingerprint", lambda spec: "3" * 64)

    def fake_verify(path, *, data_directory):
        if verification_error is not None:
            raise verification_error
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return {
            "bundle": payload["name"],
            "bundle_fingerprint_sha256": "4" * 64,
            "required_safe_max_candidates": 100,
            "exact_discovery_calibration_stages": ["exact"],
            "ready_for_calibration": True,
        }

    monkeypatch.setattr(builder_module, "verify_research_bundle", fake_verify)


def test_seal_builds_fingerprints_and_atomically_writes_verified_bundle(tmp_path: Path, monkeypatch):
    manifest_path, study_path, search_path, calibration_path, manifest = _contracts(tmp_path)
    _patch_fingerprints(monkeypatch)
    output = tmp_path / "bundle.json"

    result = seal_research_bundle(
        name="BTC real run",
        manifest_file=manifest_path,
        study_file=study_path,
        discovery_search_file=search_path,
        calibration_file=calibration_path,
        output_file=output,
        data_directory=tmp_path,
    )

    assert output.is_file()
    assert not (tmp_path / ".bundle.json.tmp").exists()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == result["bundle"]
    assert payload["manifest_file"] == "manifest.json"
    assert payload["study_file"] == "study.json"
    assert payload["discovery_search_file"] == "search.json"
    assert payload["calibration_file"] == "calibration.json"
    assert payload["dataset_fingerprint_sha256"] == manifest[
        "dataset_fingerprint_sha256"
    ]
    assert payload["study_fingerprint_sha256"] == "1" * 64
    assert payload["discovery_search_fingerprint_sha256"] == "2" * 64
    assert payload["calibration_fingerprint_sha256"] == "3" * 64
    assert result["verification"]["ready_for_calibration"] is True


def test_seal_refuses_to_overwrite_existing_bundle(tmp_path: Path, monkeypatch):
    manifest_path, study_path, search_path, calibration_path, _ = _contracts(tmp_path)
    _patch_fingerprints(monkeypatch)
    output = tmp_path / "bundle.json"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output already exists"):
        seal_research_bundle(
            name="BTC real run",
            manifest_file=manifest_path,
            study_file=study_path,
            discovery_search_file=search_path,
            calibration_file=calibration_path,
            output_file=output,
            data_directory=tmp_path,
        )
    assert output.read_text(encoding="utf-8") == "existing"


def test_seal_rejects_contract_outside_bundle_directory(tmp_path: Path, monkeypatch):
    manifest_path, study_path, search_path, _, _ = _contracts(tmp_path)
    _patch_fingerprints(monkeypatch)
    outside = tmp_path.parent / "outside-calibration.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes the research bundle directory"):
        seal_research_bundle(
            name="BTC real run",
            manifest_file=manifest_path,
            study_file=study_path,
            discovery_search_file=search_path,
            calibration_file=outside,
            output_file=tmp_path / "bundle.json",
            data_directory=tmp_path,
        )


def test_failed_verification_leaves_no_bundle_or_temporary_file(tmp_path: Path, monkeypatch):
    manifest_path, study_path, search_path, calibration_path, _ = _contracts(tmp_path)
    _patch_fingerprints(monkeypatch, verification_error=ValueError("lineage failed"))
    output = tmp_path / "bundle.json"

    with pytest.raises(ValueError, match="lineage failed"):
        seal_research_bundle(
            name="BTC real run",
            manifest_file=manifest_path,
            study_file=study_path,
            discovery_search_file=search_path,
            calibration_file=calibration_path,
            output_file=output,
            data_directory=tmp_path,
        )
    assert not output.exists()
    assert not (tmp_path / ".bundle.json.tmp").exists()


def test_bundle_cli_parses_seal_contract():
    args = build_parser().parse_args(
        [
            "seal",
            "--name",
            "BTC run",
            "--manifest",
            "manifest.json",
            "--study",
            "study.json",
            "--search",
            "search.json",
            "--calibration",
            "calibration.json",
            "--data-directory",
            "data",
            "--output",
            "bundle.json",
        ]
    )
    assert args.command == "seal"
    assert args.name == "BTC run"
    assert args.output == "bundle.json"
