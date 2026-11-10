"""Run the public test tree on a Linux Docker host using an exact local image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="immutable sha256 image ID")
    parser.add_argument("--archives", required=True, type=Path)
    parser.add_argument("--results-parent", required=True, type=Path)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--test", action="append", help="pytest node to repeat under tests/ (default: entire tree)")
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image):
        parser.error("supply the image ID from docker image inspect")
    for node in args.test or []:
        file = node.partition("::")[0]
        if not re.fullmatch(r"tests/(?:[A-Za-z_0-9]+/)*test_[A-Za-z_0-9]+\.py", file):
            parser.error("expected a pytest file or node under tests/")
    for directory in (args.archives, args.results_parent):
        if directory.resolve() != directory or not directory.is_dir():
            parser.error("archives and results-parent must be absolute, existing physical directories")
    repo = Path(__file__).resolve().parents[1]
    output = Path(tempfile.mkdtemp(prefix="public-regression-", dir=args.results_parent))
    print(output, flush=True)
    scratch = output / "work"
    scratch.mkdir(mode=0o700)
    for path in (scratch, *(scratch / n for n in ("home", "config", "cache"))):
        path.mkdir(mode=0o700, exist_ok=True)
        if os.getuid() != 10001 or os.getgid() != 10001:
            os.chown(path, 10001, 10001)
    fixture_dir = output / "archives"
    fixture_dir.mkdir(mode=0o755)
    # Stage exactly the checkout's registered files. The image independently
    # verifies its own registry before it can run any tests.
    pins = runpy.run_path(str(repo / "tests/ops/historical_artifacts.py"))["PINS"]
    for name, expected_hash in pins.items():
        source = args.archives / name
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2_000_000:
                parser.error(f"invalid historical test input: {name}")
            raw = stream.read(2_000_001)
        if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != expected_hash:
            parser.error(f"historical test input hash mismatch: {name}")
        target = fixture_dir / name
        target.write_bytes(raw)
        target.chmod(0o444)
    fixture_dir.chmod(0o555)

    command = [
        "docker", "run", "--rm", "--init", "--pull=never", "--network=none", "--read-only",
        "--user=10001:10001", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--security-opt", f"seccomp={repo / 'config/browser-seccomp.json'}",
        "--cpus=1.5", "--memory=1500m", "--memory-swap=1500m", "--pids-limit=256",
        "--shm-size=256m", "--tmpfs", "/tmp:size=192m,mode=1777",
        "-e", "TMPDIR=/s", "-e", "HOME=/s/home", "-e", "XDG_CONFIG_HOME=/s/config",
        "-e", "XDG_CACHE_HOME=/s/cache", "-e", "LIGHTHOUSE_TEST_ARTIFACT_DIR=/artifacts",
    ]
    for source, dest, options in (
        (scratch, "/s", ""), (fixture_dir, "/artifacts", ",readonly"),
        (repo / "config/hub.example.toml", "/opt/lighthouse-hub/config/hub.toml", ",readonly"),
    ):
        if "," in str(source):
            parser.error("Docker mount source may not contain a comma")
        command.extend(["--mount", f"type=bind,src={source},dst={dest}{options}"])
    preflight = ("from tests.ops.historical_artifacts import PINS, historical_artifact; "
                 "[historical_artifact(name) for name in PINS]")
    command.extend([args.image, "sh", "-ec", 'python -c "$1"; shift; exec "$@"', "regression",
                    preflight, "timeout", "--kill-after=30s", "2h", "xvfb-run", "-a", "python", "-m", "pytest",
                    "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
                    "--basetemp=/s/b", "--durations=10"])
    if args.collect_only:
        command.append("--collect-only")
    else:
        command.append("--junitxml=/s/full.xml")
    command.extend(args.test or ["tests"])
    with (output / "suite.log").open("xb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    report = {"image": args.image, "mode": "collect" if args.collect_only else "run",
              "selection": args.test or ["tests"], "exit_code": result.returncode,
              "junit_present": (scratch / "full.xml").is_file()}
    (output / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
