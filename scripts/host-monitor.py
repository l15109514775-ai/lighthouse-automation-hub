"""Root-only Docker readout. Export selected fields, never inspect or expose container Env."""
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import time

root=Path('/opt/lighthouse-hub')
assert os.geteuid()==0
raw=subprocess.check_output(['/usr/bin/docker','ps','-a','--filter','label=com.docker.compose.project=lighthouse-hub',
                              '--format','{{.ID}}'],text=True,timeout=10)
# The isolated publisher intentionally belongs to a separate Compose project.
# Keep its project AND service scoped; do not expose every host container.
publisher_raw=subprocess.check_output(['/usr/bin/docker','ps','-a',
    '--filter','label=com.docker.compose.project=lighthouse-publication',
    '--filter','label=com.docker.compose.service=replay-publication-worker',
    '--format','{{.ID}}'],text=True,timeout=10)
ids=list(dict.fromkeys(raw.split()+publisher_raw.split()))
assert all(re.fullmatch('[0-9a-f]{12,64}',value) for value in ids)
containers=[]
for ident in ids:
    fields=subprocess.check_output(['/usr/bin/docker','inspect','--format',
        '{{.Name}}|{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}not_configured{{end}}',
        ident],text=True,timeout=10).strip().split('|')
    name,state,exit_code,restarts,health=fields
    containers.append({'name':name.lstrip('/'),'state':state,'exit_code':int(exit_code),
                       'restart_count':int(restarts),'health':health})
payload={'checked_at':time.time(),'containers':containers,'expected_services':5,
         'source':'root-owned-host-docker-cli-readonly-snapshot'}
descriptor,name=tempfile.mkstemp(prefix='host-status-',suffix='.json',dir=root/'data')
with os.fdopen(descriptor,'w') as stream:
    json.dump(payload,stream)
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(name,0o644)
os.replace(name,root/'data/host-status.json')
print('HOST_STATUS_REFRESHED; selected state fields only')
