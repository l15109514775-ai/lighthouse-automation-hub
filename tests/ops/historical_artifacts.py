"""Locate genuine historical test inputs; never download or regenerate proofs."""
import hashlib
import os
import re
import tarfile
from pathlib import Path

PINS = {
    'historical-deployment-source-inputs.tar': '7903c13bb927b3a03d6d3c5b3213365c298b871381ae35e8c9dc00b5795bc93d',
    'historical-retention-v2c-source-inputs.tar': '070517adad0216df3eb9e2da5bf599bca41d0fe25b9a266f9c502044da898e13',
    'video-natural-formats-v3-overlay.tar.gz': 'd2cbbacb026f89f853c29e5bb3fc1d9a77ec58e4b69e06300abff146dc0a2994',
    'video-source-tracks-v2-overlay.tar.gz': '51b6f980f25bfe18da248625939873dc33078125ecbe9c2cd49b7624aa9f15ff',
}


def historical_artifact(name):
    if name not in PINS:
        raise ValueError('HISTORICAL_ARTIFACT_NOT_REGISTERED')
    directory = Path(os.environ.get('LIGHTHOUSE_TEST_ARTIFACT_DIR', Path(__file__).resolve().parents[4] / 'outputs'))
    path = directory / name
    if not directory.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('HISTORICAL_ARTIFACT_PATH_UNSAFE')
    if not path.is_file():
        raise FileNotFoundError('HISTORICAL_ARTIFACT_MISSING:' + name)
    if not 0 < path.stat().st_size <= 2_000_000:
        raise ValueError('HISTORICAL_ARTIFACT_SIZE_INVALID')
    with path.open('rb') as stream:
        raw = stream.read(2_000_001)
    if hashlib.sha256(raw).hexdigest() != PINS[name]:
        raise ValueError('HISTORICAL_ARTIFACT_HASH_MISMATCH')
    return path


def historical_member(archive, name, expected_sha256):
    """Read one pinned regular member as data; never extract or execute it."""
    from pathlib import PurePosixPath
    entry = PurePosixPath(name)
    if (entry.is_absolute() or str(entry) != name or '..' in entry.parts
            or '\\' in name or not re.fullmatch('[a-f0-9]{64}', expected_sha256)):
        raise ValueError('HISTORICAL_MEMBER_INVALID')
    with tarfile.open(historical_artifact(archive)) as source:
        matching = [m for m in source.getmembers() if m.name == name]
        if len(matching) != 1 or not matching[0].isfile() or not 0 < matching[0].size <= 262144:
            raise ValueError('HISTORICAL_MEMBER_INVALID')
        with source.extractfile(matching[0]) as stream:
            raw = stream.read(262145)
        if len(raw) != matching[0].size or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('HISTORICAL_MEMBER_HASH_MISMATCH')
    return raw
