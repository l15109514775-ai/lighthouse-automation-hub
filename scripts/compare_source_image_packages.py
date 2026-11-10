"""Compare the current Linux image with a qualified source-build package inventory."""

import argparse
from importlib import metadata
import json
from pathlib import Path
import subprocess


def changes(expected, actual):
    return {
        name: {"expected": expected.get(name), "installed": actual.get(name)}
        for name in sorted(expected.keys() | actual.keys())
        if expected.get(name) != actual.get(name)
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args()
    expected = json.loads(args.inventory.read_bytes())
    for family in ("os", "python"):
        versions = expected[family]
        if not isinstance(versions, dict) or not versions or not all(
            isinstance(v, str) and v for v in versions.values()
        ):
            parser.error(f"invalid {family} baseline")

    # Use the same query and interpreter as the qualified inventory generator.
    records = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}\\t${Version}\\n"], text=True
    )
    os_versions = dict(line.split("\t", 1) for line in records.splitlines())
    python_versions = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name in python_versions and python_versions[name] != dist.version:
            parser.error(f"multiple installed versions for {name}")
        python_versions[name] = dist.version

    drift = {
        "os": changes(expected["os"], os_versions),
        "python": changes(expected["python"], python_versions),
    }
    report = {
        "os_packages": len(os_versions),
        "python_distributions": len(python_versions),
        "differences": drift,
    }
    print(json.dumps(report, sort_keys=True))
    return int(any(drift.values()))


if __name__ == "__main__":
    raise SystemExit(main())
