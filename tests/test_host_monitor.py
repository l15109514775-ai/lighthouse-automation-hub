import json
import os
import time
from services.hub.monitoring import host_snapshot


def test_stale_or_mutable_host_snapshot_not_reported_as_live(runtime):
    settings,_=runtime
    file=settings.root/'data/host-status.json'
    file.write_text(json.dumps({'checked_at':time.time()-400,'containers':[]}),'utf-8')
    file.chmod(0o644)
    assert host_snapshot(settings)['available'] is False


def test_valid_snapshot_has_no_docker_control_capability(runtime):
    settings,_=runtime
    file=settings.root/'data/host-status.json'
    file.write_text(json.dumps({'checked_at':time.time(),'containers':[{'name':'lighthouse-hub-api-1','state':'running'}]}),'utf-8')
    file.chmod(0o644)
    snapshot=host_snapshot(settings)
    if os.name=='posix' and file.stat().st_uid!=0:
        assert snapshot['available'] is False
    else:
        assert snapshot['available'] is True
        assert snapshot['containers'][0]['state']=='running'
    assert 'socket' not in json.dumps(snapshot)
