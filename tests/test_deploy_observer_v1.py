"""Reject observer boundary changes and prove rollback targets only observer.

These tests use owned Docker-inspect fixtures; they never invoke Docker/systemd.
The root operator performs real Linux qualification separately.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('deploy_observer', Path(__file__).parents[1]/'scripts/ops/deploy_observer_v1.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def container():
    return {'Id':'owned-observer', 'Image':deploy.IMAGE,
      'Config': {'User':'10001:10001','Labels':{deploy.LABEL:deploy.ROLE},
        'Entrypoint':['python'],'Cmd':['-I','/observer/observer_mcp_v1.py'],
        'Env':['PATH=/usr/local/bin:/usr/bin','HUB_OBSERVER_SNAPSHOT=/snapshot/status.json','PYTHONDONTWRITEBYTECODE=1'],
        'Healthcheck':{'Test':['NONE']}},
      'HostConfig':{'ReadonlyRootfs':True,'NetworkMode':'none','CapDrop':['ALL'],
        'SecurityOpt':['no-new-privileges'],'IpcMode':'private','Memory':268435456,'NanoCpus':250000000,'PidsLimit':64,
        'Tmpfs':{'/tmp':'rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001'},
        'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}},'RestartPolicy':{'Name':'unless-stopped'}},
      'Mounts':[{'Type':'bind','Source':str(deploy.SNAPSHOT),'Destination':'/snapshot','RW':False},
        {'Type':'bind','Source':str(deploy.CODE/deploy.SOURCES[1]),'Destination':'/observer/observer_mcp_v1.py','RW':False}],
      'NetworkSettings':{'Networks':{'none':{}}},'State':{'Running':True}}


@pytest.mark.parametrize('change',['database','socket','write','port','privileged','capability','network','host_pid','secret','extra_env','healthcheck','unbounded_memory'])
def test_changed_container_scope_is_refused(change):
    item=container();deploy.verify_container(item,['PATH=/usr/local/bin:/usr/bin'])
    if change in ('database','socket'):
        item['Mounts'].append({'Type':'bind','Source':'/opt/lighthouse-hub/data' if change=='database' else '/var/run/docker.sock','Destination':'/private','RW':False})
    elif change=='write':item['Mounts'][0]['RW']=True
    elif change=='port':item['HostConfig']['PortBindings']={'8000/tcp':[{'HostPort':'8000'}]}
    elif change=='privileged':item['HostConfig']['Privileged']=True
    elif change=='capability':item['HostConfig']['CapAdd']=['SYS_ADMIN']
    elif change=='network':item['NetworkSettings']['Networks']['bridge']={}
    elif change=='host_pid':item['HostConfig']['PidMode']='host'
    elif change=='secret':item['Config']['Env'].append('X_BEARER_TOKEN=owned-secret')
    elif change=='extra_env':item['Config']['Env'].append('HUB_ROOT=/private')
    elif change=='healthcheck':item['Config']['Healthcheck']['Test']=['CMD','curl','http://api']
    elif change=='unbounded_memory':item['HostConfig']['Memory']=0
    with pytest.raises(ValueError):deploy.verify_container(item,['PATH=/usr/local/bin:/usr/bin'])


def test_rollback_preserves_files_and_never_restarts_or_stops_core(monkeypatch):
    item=container();calls=[];before={'owned-core':'running-unchanged'}
    monkeypatch.setattr(deploy,'inspect_container',lambda name:deepcopy(item))
    monkeypatch.setattr(deploy,'verify_container',lambda actual:None)
    monkeypatch.setattr(deploy,'run',lambda args,**kwargs:calls.append(args) or '')
    monkeypatch.setattr(deploy,'core_fingerprint',lambda:before)
    result=deploy.rollback({'core_before':before})
    assert calls==[['systemctl','disable','--now',deploy.TIMER],['systemctl','stop',deploy.SERVICE],
                   ['docker','stop','--time','10','owned-observer']]
    assert result['business_files_deleted']==0 and result['core_unchanged'] is True


def test_rollback_refuses_to_touch_unrecognized_same_name_container(monkeypatch):
    item=container();item['Config']['User']='root';calls=[]
    monkeypatch.setattr(deploy,'inspect_container',lambda name:item)
    verify=deploy.verify_container
    monkeypatch.setattr(deploy,'verify_container',lambda actual:verify(actual,['PATH=/usr/local/bin:/usr/bin']))
    monkeypatch.setattr(deploy,'run',lambda args,**kwargs:calls.append(args) or '')
    with pytest.raises(ValueError):deploy.rollback({'core_before':{}})
    assert calls==[]
