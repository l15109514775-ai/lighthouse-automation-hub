"""Image-build-only removal of explicit X visual entry points; no runtime data."""
import argparse
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat

ROOT=Path('/opt/lighthouse-hub')
VERSION='x-visual-runtime-retired-v1'
PATHS=(
    'adapters/x_web.py','services/hub/x_web_discovery.py','adapters/browser_x.mjs',
    'adapters/browser_expiry_preflight.mjs','scripts/capture-extended-acceptance.mjs',
    'scripts/login-gui/browser.mjs','scripts/login-gui/session.py',
    'scripts/login-gui/lighthouse-x-login-gui.service','scripts/login-gui/Dockerfile',
)
PROTECTED=(
    'adapters/browser_publisher.mjs','adapters/bilibili.py','services/hub/publishing.py',
    'compositor/package.json','compositor/src/renderer.mjs','compositor/template.json',
)


def regular(root,relative):
    pure=PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or '..' in pure.parts:raise RuntimeError('RETIREMENT_PATH_INVALID')
    current=root
    if (not root.is_absolute() or root.is_symlink() or any(p.is_symlink() for p in root.parents)
            or not root.is_dir()):raise RuntimeError('RETIREMENT_ROOT_INVALID')
    for part in pure.parts:
        current=current/part
        if current.is_symlink():raise RuntimeError('RETIREMENT_SYMLINK_REFUSED')
    if not stat.S_ISREG(current.lstat().st_mode):raise RuntimeError('RETIREMENT_REGULAR_FILE_REQUIRED')
    return current


def descriptor(root,relative):
    return {'path':relative,'sha256':hashlib.sha256(regular(root,relative).read_bytes()).hexdigest()}


def verify_upstream(root):
    manifest=json.loads(regular(root,'config/upstream-lock.json').read_text('utf-8'))
    if set(manifest)!={'compositor','vendor/subtitle-cloud-worker'}:raise RuntimeError('RETIREMENT_UPSTREAM_INVALID')
    count=0
    for folder,entry in manifest.items():
        for relative,expected in entry['files'].items():
            if not re.fullmatch('[a-f0-9]{64}',expected) or descriptor(root,folder+'/'+relative)['sha256']!=expected:
                raise RuntimeError('RETIREMENT_UPSTREAM_CHANGED')
            count+=1
    if count!=281:raise RuntimeError('RETIREMENT_UPSTREAM_COUNT_INVALID')
    return count


def retire(root,*,base_image):
    """An explicit root argument supports owned temporary-root tests only.

    The executable CLI below has no root override and always uses /opt.
    Failure at any point makes the image build fail; no runtime rollback occurs.
    """
    root=Path(root)
    if not re.fullmatch(r'(?:[^@\s]+@)?sha256:[a-f0-9]{64}',base_image):raise RuntimeError('PINNED_BASE_IMAGE_REQUIRED')
    marker=root/'x-visual-retired.json'
    if marker.exists() or marker.is_symlink():raise RuntimeError('RETIREMENT_MARKER_ALREADY_EXISTS')
    count=verify_upstream(root)
    protected=[descriptor(root,p) for p in PROTECTED]
    removed=[descriptor(root,p) for p in PATHS]
    # Preflight all files before the first unlink. Each unlink is direct and
    # bounded; no recursion, glob, directory removal or data/profile access.
    for item in removed:
        file=regular(root,item['path'])
        if descriptor(root,item['path'])!=item:raise RuntimeError('RETIREMENT_TARGET_CHANGED')
        file.unlink()
    if verify_upstream(root)!=count or [descriptor(root,p) for p in PROTECTED]!=protected:
        raise RuntimeError('RETIREMENT_PROTECTED_FILES_CHANGED')
    report={'version':VERSION,'base_image':base_image,'paths':removed,'protected':protected,
            'upstream_files_verified':count}
    encoded=(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
    with marker.open('xb') as stream:
        os.chmod(marker,0o444);stream.write(encoded);stream.flush();os.fsync(stream.fileno())
    if os.name!='nt':
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image',required=True)
    args=parser.parse_args()
    result=retire(ROOT,base_image=args.base_image)
    print(json.dumps({'version':result['version'],'removed_files':len(result['paths']),
                      'upstream_files_verified':result['upstream_files_verified']}))
