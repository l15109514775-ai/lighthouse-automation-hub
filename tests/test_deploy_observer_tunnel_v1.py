"""Owned container-inspect fixtures; no Docker, network, systemd or real key."""
import importlib.util
from pathlib import Path
import pytest


spec=importlib.util.spec_from_file_location('deploy_observer_tunnel',
    Path(__file__).parents[1]/'scripts/ops/deploy_observer_tunnel_v1.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
IMAGE_ENV=['PATH=/usr/local/bin:/usr/bin:/bin','LANG=C.UTF-8']


def container():
    return {'Image':deploy.IMAGE,
        'Config':{'User':'10001:10001','Labels':{'io.lighthouse.observer.role':'tunnel-v1'},
            'Entrypoint':['/client/tunnel-client'],'Cmd':['run',*deploy.client_flags()],
            'Env':list(IMAGE_ENV),'Healthcheck':{'Test':['NONE']}},
        'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL'],'NetworkMode':'bridge',
            'SecurityOpt':['no-new-privileges'],'RestartPolicy':{'Name':'unless-stopped'},
            'Memory':268435456,'NanoCpus':250000000,'PidsLimit':64,'IpcMode':'private',
            'LogConfig':{'Type':'local','Config':{'max-size':'2m','max-file':'3'}},
            'Tmpfs':{'/tmp':'rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001'}},
        'Mounts':[{'Type':'bind','Source':str(source),'Destination':target,'RW':False}
            for source,target in ((deploy.BINARY,'/client/tunnel-client'),
                (deploy.READER,'/observer/observer_mcp_v1.py'),(deploy.SNAPSHOT,'/snapshot'),
                (deploy.KEYFILE,'/run/secrets/control-plane.key'))],
        'NetworkSettings':{'Networks':{'bridge':{}}}}


def test_fixed_nonroot_stdio_reader_and_four_readonly_mounts_have_no_secret_environment(monkeypatch):
    def forbidden(*args,**kwargs): pytest.fail('An owned fixture must not invoke Docker')
    monkeypatch.setattr(deploy,'run',forbidden)
    item=container();deploy.verify_container(item,IMAGE_ENV)
    assert len(item['Mounts'])==4 and all(not row['RW'] for row in item['Mounts'])
    args=deploy.client_flags();key=args[args.index('--control-plane.api-key')+1]
    assert key=='file:/run/secrets/control-plane.key'
    assert args[args.index('--health.listen-addr')+1]=='127.0.0.1:8091'
    assert '--allow-remote-ui=false' in args and '--open-web-ui=false' in args
    assert args[args.index('--mcp.command')+1]==deploy.CHILD
    assert '/usr/bin/env -i ' in deploy.CHILD and ' -I -B /observer/observer_mcp_v1.py' in deploy.CHILD
    assert not any('KEY=' in value for value in item['Config']['Env'])
    assert not item['HostConfig'].get('PortBindings')


@pytest.mark.parametrize('change',('harpoon_env','profile_env','mcp_env','secret_env','database','socket',
    'writable','port','privileged','extra_network','root_user','command','unbounded_memory'))
def test_same_name_container_with_broader_access_or_execution_scope_is_refused(change):
    item=container();deploy.verify_container(item,IMAGE_ENV)
    env={'harpoon_env':'HARPOON_TARGETS=label=owned,url=https://example.invalid,desc=owned',
        'profile_env':'TUNNEL_CLIENT_PROFILE_FILE=/tmp/owned-profile.yaml',
        'mcp_env':'MCP_SERVER_URL=https://example.invalid/mcp',
        'secret_env':'CONTROL_PLANE_API_KEY=owned-placeholder'}
    if change in env: item['Config']['Env'].append(env[change])
    elif change in ('database','socket'):
        item['Mounts'].append({'Type':'bind','Source':'/opt/lighthouse-hub/data' if change=='database' else '/var/run/docker.sock',
            'Destination':'/private','RW':False})
    elif change=='writable': item['Mounts'][2]['RW']=True
    elif change=='port': item['HostConfig']['PortBindings']={'8091/tcp':[{'HostPort':'8091'}]}
    elif change=='privileged': item['HostConfig']['Privileged']=True
    elif change=='extra_network': item['NetworkSettings']['Networks']['lighthouse-hub_default']={}
    elif change=='root_user': item['Config']['User']='0:0'
    elif change=='command': item['Config']['Cmd']=['run','--mcp.command','/bin/sh']
    elif change=='unbounded_memory': item['HostConfig']['Memory']=0
    with pytest.raises(ValueError,match='EXISTING_TUNNEL_SCOPE_CHANGED'):
        deploy.verify_container(item,IMAGE_ENV)
