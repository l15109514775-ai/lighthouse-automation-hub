import json
import os
import time


def host_snapshot(settings):
    file=settings.root / 'data/host-status.json'
    try:
        info=file.stat()
        if info.st_size>100_000 or (os.name=='posix' and (info.st_uid!=0 or info.st_mode & 0o022)):
            return {'available':False,'reason':'untrusted_host_snapshot'}
        data=json.loads(file.read_text('utf-8'))
        if not isinstance(data.get('checked_at'),(int,float)) or not 0<=time.time()-data['checked_at']<=180:
            return {'available':False,'reason':'stale_host_snapshot'}
        if not isinstance(data.get('containers'),list) or len(data['containers'])>20:
            return {'available':False,'reason':'invalid_host_snapshot'}
        return {'available':True,**data}
    except (OSError,ValueError,TypeError):
        return {'available':False,'reason':'host_snapshot_not_available'}
