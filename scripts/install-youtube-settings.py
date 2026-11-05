"""Install only a YouTube API key, preserving other secrets and the source file."""
from pathlib import Path
import os
import re
import sys
import tempfile


def merge(current, incoming):
    lines=[line for line in incoming.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if len(lines)!=1 or not lines[0].startswith('YOUTUBE_API_KEY='):
        raise ValueError('ONLY_YOUTUBE_API_KEY_ALLOWED')
    key=lines[0].partition('=')[2]
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,200}',key):
        raise ValueError('YOUTUBE_API_KEY_FORMAT_INVALID')
    result=[]
    found=False
    for line in current.splitlines():
        if line.startswith('YOUTUBE_API_KEY='):
            if found:
                raise ValueError('DUPLICATE_YOUTUBE_KEY_SETTING')
            line='YOUTUBE_API_KEY='+key
            found=True
        result.append(line)
    if not found:
        result.append('YOUTUBE_API_KEY='+key)
    return '\n'.join(result)+'\n'


def main():
    if not hasattr(os,'geteuid') or os.geteuid()!=0 or len(sys.argv)!=2:
        raise SystemExit('ROOT_AND_PRIVATE_SOURCE_FILE_REQUIRED')
    source=Path(sys.argv[1])
    target=Path('/etc/lighthouse-hub/secrets.env')
    if (source.is_symlink() or target.is_symlink() or target.parent.is_symlink() or
            source.stat().st_mode & 0o077 or target.stat().st_uid!=0 or target.stat().st_mode & 0o077 or
            target.parent.stat().st_uid!=0 or target.parent.stat().st_mode & 0o077):
        raise SystemExit('SECRET_FILE_PERMISSIONS_INVALID')
    updated=merge(target.read_text('utf-8'),source.read_text('utf-8-sig'))
    fd,name=tempfile.mkstemp(prefix='youtube-settings-',dir=target.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,target)
        directory=os.open(target.parent,os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print('YOUTUBE_SETTINGS_INSTALLED: values suppressed; other credentials unchanged; recreate services to reload')


if __name__=='__main__':
    main()
