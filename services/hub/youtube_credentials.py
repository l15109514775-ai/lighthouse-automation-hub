"""Restricted YouTube Secret loading, independent of any worker or publisher."""
import os
from pathlib import Path
import re
import stat

from .errors import HubError


def load_youtube_key(file):
    """Load only the already-authorized YouTube key into the Python parent.

    ReplayBrowserSession builds its child environment from an explicit public
    whitelist; neither this variable nor the key-file path is passed to Node.
    """
    path=Path(file)
    try:
        if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError()
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
        try:
            before=os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or not 1<=before.st_size<=512 or
                    os.name=='posix' and (before.st_uid not in (0,10001) or stat.S_IMODE(before.st_mode) not in (0o400,0o440,0o600,0o640))):
                raise ValueError()
            body=os.read(fd,513);after=os.fstat(fd)
            if len(body)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in
                    ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):raise ValueError()
        finally:os.close(fd)
        text=body.decode('ascii').strip()
        if text.startswith('YOUTUBE_API_KEY='):text=text[len('YOUTUBE_API_KEY='):]
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,160}',text):raise ValueError()
        os.environ['YOUTUBE_API_KEY']=text
    except (OSError,UnicodeError,ValueError):
        raise HubError('REPLAY_RUNTIME_YOUTUBE_KEY_FILE_INVALID',review=True) from None
