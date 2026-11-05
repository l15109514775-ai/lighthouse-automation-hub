"""Resolve once in the reviewed environment, then validate this lock on Ubuntu."""
from importlib.metadata import distributions
from pathlib import Path

root = Path(__file__).resolve().parents[1]
rows = []
for package in distributions():
    name = package.metadata['Name']
    if name.lower() in {'lighthouse-automation-hub','pip','setuptools','wheel'}:
        continue
    marker = '; sys_platform == "win32"' if name.lower() == 'pywin32' else ''
    rows.append(f'{name}=={package.version}{marker}')
(root / 'requirements.lock').write_text('# Resolved versions; Ubuntu installation and tests are recorded separately.\n' + '\n'.join(sorted(set(rows), key=str.lower)) + '\n', 'utf-8')
