"""Produce checksums and factual build provenance; never claim an unsigned build is signed."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("requirements-win-py314.lock", "requirements-build-win-py314.lock")


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def app_version(root=ROOT):
    tree = ast.parse((Path(root) / "app" / "appinfo.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+", value):
                return value
    raise ValueError("appinfo.py must declare a semantic release version")


def validate_tag(tag, version):
    if tag != "v" + version or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Release tag must exactly match v<appinfo version>")


def git_info(root, tag=None, require_clean=False):
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    if require_clean and dirty:
        raise ValueError("Release source must have a clean working tree")
    if tag and git("rev-parse", "--verify", "refs/tags/" + tag + "^{commit}") != commit:
        raise ValueError("Release tag does not identify the checked-out source commit")
    return {"commit": commit, "tag": tag, "dirty": dirty}


def authenticode(path):
    if sys.platform != "win32":
        return {"status": "NotChecked", "reason": "Authenticode requires Windows"}
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return {"status": "NotChecked", "reason": "PowerShell unavailable"}
    command = ("$s=Get-AuthenticodeSignature -LiteralPath $env:DISCORDANT_ARTIFACT; "
               "@{status=$s.Status.ToString(); signer_thumbprint="
               "$(if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{$null})} | ConvertTo-Json -Compress")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            env=dict(os.environ, DISCORDANT_ARTIFACT=str(Path(path).resolve())),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout.lstrip("\ufeff"))


def browser_info(app_dir):
    app_dir = Path(app_dir).resolve()
    manifests = [p for p in app_dir.rglob("browsers.json") if p.resolve().is_relative_to(app_dir)]
    if len(manifests) != 1:
        raise ValueError("Expected exactly one bundled Playwright browsers.json")
    manifest = manifests[0]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    declared = [b for b in data["browsers"] if b.get("name") in ("chromium", "winldd")]
    folders = sorted(p.name for p in (app_dir / "browser").iterdir() if p.is_dir())
    chromium = next((b for b in declared if b["name"] == "chromium"), None)
    if not chromium or "chromium-" + str(chromium["revision"]) not in folders:
        raise ValueError("Bundled Chromium revision does not match Playwright")
    executables = []
    for path in sorted((app_dir / "browser").rglob("*.exe")):
        if not path.resolve().is_relative_to(app_dir):
            raise ValueError("Browser file escapes the packaged application")
        executables.append({"file": path.relative_to(app_dir).as_posix(), "sha256": sha256(path)})
    if not executables:
        raise ValueError("Bundled browser executables are missing")
    return {"manifest_sha256": sha256(manifest), "declared": declared,
            "bundled_directories": folders, "executables": executables}


def write_metadata(root, artifacts, app_dir, output_dir, tag=None, require_clean=False):
    root, output_dir = Path(root), Path(output_dir)
    version = app_version(root)
    if tag:
        validate_tag(tag, version)
    artifacts = [Path(p) for p in artifacts]
    if not artifacts or len({p.name for p in artifacts}) != len(artifacts):
        raise ValueError("Artifacts need unique basenames")
    data = {
        "format_version": 1, "application": "DiscordAnt", "version": version,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": git_info(root, tag, require_clean),
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "system": platform.system(), "machine": platform.machine()},
        "dependencies": sorted([{"name": d.metadata["Name"], "version": d.version}
                                for d in importlib.metadata.distributions()], key=lambda d: d["name"].lower()),
        "dependency_locks": [{"file": name, "sha256": sha256(root / name)} for name in LOCKS],
        "browser": browser_info(app_dir),
        "artifacts": [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p),
                       "authenticode": authenticode(p)} for p in artifacts],
        "provenance_note": "Build metadata and checksums; this JSON is not a signed attestation.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "provenance.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    lines = [f"{item['sha256']}  {item['file']}" for item in data["artifacts"]]
    lines.append(f"{sha256(target)}  {target.name}")
    sums = output_dir / "SHA256SUMS"
    temporary = sums.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, sums)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, default=ROOT / "dist" / "DiscordAnt")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--tag")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    write_metadata(ROOT, args.artifact, args.app_dir, args.output_dir, args.tag, args.require_clean)
    print("Wrote provenance.json and SHA256SUMS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
