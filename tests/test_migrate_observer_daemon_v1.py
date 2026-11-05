"""Fault-injection tests for collector-only migration; no real Docker/systemd."""
import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('migrate_observer',ROOT/'scripts/ops/migrate_observer_daemon_v1.py')
migrate=importlib.util.module_from_spec(spec);spec.loader.exec_module(migrate)


def test_old_unit_hash_guard_matches_previously_deployed_linux_bytes():
    spec=importlib.util.spec_from_file_location('old_observer',ROOT/'scripts/ops/deploy_observer_v1.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    old.APP=PurePosixPath('/opt/lighthouse-hub');old.CODE=PurePosixPath('/opt/lighthouse-observer-v1')
    old.SNAPSHOT=PurePosixPath('/var/lib/lighthouse-observer-v1/snapshot')
    assert {name:migrate.sha(raw) for name,raw in old.units().items()}==migrate.OLD_UNIT_SHA
    daemon=migrate.service_bytes().decode()
    for directive in ('ProtectSystem=strict','PrivateNetwork=true','RestrictAddressFamilies=AF_UNIX','IPAddressDeny=any',
            'ReadOnlyPaths=/opt/lighthouse-hub /opt/lighthouse-observer-v1',
            'ReadWritePaths=/var/lib/lighthouse-observer-v1/snapshot','Restart=on-failure','RestartSec=5',
            'StartLimitIntervalSec=0','TimeoutStopSec=10'):
        assert directive in daemon.splitlines()
    assert daemon.count('ReadWritePaths=')==1 and 'ExecStartPre=' not in daemon


@pytest.mark.parametrize('change',['none','service_changed','timer_changed','old_override','new_override','collector_changed','reader_changed'])
def test_preflight_refuses_changed_units_and_unreviewed_sources(tmp_path,monkeypatch,change):
    code=tmp_path/'code';state=tmp_path/'state';source=tmp_path/'source.py';commands=[]
    monkeypatch.setattr(migrate,'CODE',str(code));monkeypatch.setattr(migrate,'STATE',str(state))
    monkeypatch.setattr(migrate,'check_host',lambda:None)
    monkeypatch.setattr(migrate,'unit_path',lambda name:tmp_path/name)
    old_raw={name:('owned '+name).encode() for name in migrate.OLD_UNIT_SHA}
    monkeypatch.setattr(migrate,'OLD_UNIT_SHA',{name:migrate.sha(raw) for name,raw in old_raw.items()})
    monkeypatch.setattr(migrate,'COLLECTOR_SHA',migrate.sha(b'owned collector'))
    monkeypatch.setattr(migrate,'READER_SHAS',{migrate.sha(b'owned reader')})
    raw={source:b'owned daemon',code/'observer_snapshot_v1.py':b'owned collector',code/'observer_mcp_v1.py':b'owned reader',
         **{tmp_path/name:value for name,value in old_raw.items()}}
    if change=='service_changed':raw[tmp_path/migrate.OLD_SERVICE]=b'changed unit'
    if change=='timer_changed':raw[tmp_path/migrate.OLD_TIMER]=b'changed timer'
    if change=='collector_changed':raw[code/'observer_snapshot_v1.py']=b'unknown collector'
    if change=='reader_changed':raw[code/'observer_mcp_v1.py']=b'unknown reader'
    monkeypatch.setattr(migrate,'root_file',lambda path:raw[Path(path)])
    def safe(path):
        path=Path(path);mode=0o755 if path==code else 0o2750 if path==state/'snapshot' else 0o750
        return SimpleNamespace(stat=lambda:SimpleNamespace(st_mode=0o040000|mode,st_uid=0,st_gid=0 if path==code else 10001))
    monkeypatch.setattr(migrate,'safe_path',safe)
    def unit(name):
        return {'FragmentPath':'' if name==migrate.SERVICE else str(tmp_path/name),
                'DropInPaths':'/owned/override.conf' if change=='old_override' and name==migrate.OLD_SERVICE or change=='new_override' and name==migrate.SERVICE else '',
                'ActiveState':'active','UnitFileState':'enabled','MainPID':'24'}
    monkeypatch.setattr(migrate,'unit_state',unit)
    monkeypatch.setattr(migrate,'container_fingerprint',lambda:{'owned':'stable'})
    monkeypatch.setattr(migrate,'run',lambda args:commands.append(args))
    if change=='none':
        plan,files=migrate.preflight(source,migrate.sha(b'owned daemon'))
        assert plan['reader_sha256']==migrate.sha(b'owned reader') and len(files)==2
    else:
        with pytest.raises(ValueError):migrate.preflight(source,migrate.sha(b'owned daemon'))
    assert commands==[]


@pytest.fixture
def harness(monkeypatch):
    calls=[];created=[];before={'owned-business-and-readers':'unchanged'}
    state={'timer_active':True,'daemon_active':False,'fail':None,'reverted':False}
    def run(args):
        calls.append(args)
        if args==['systemctl','disable','--now',migrate.OLD_TIMER]:state['timer_active']=False
        if args==['systemctl','enable','--now',migrate.SERVICE]:state['daemon_active']=True
        if args==['systemctl','disable','--now',migrate.SERVICE]:state['daemon_active']=False
        if args==['systemctl','enable','--now',migrate.OLD_TIMER]:state['timer_active']=True;state['reverted']=True
        if args==state['fail']:raise ValueError('OWNED_COMMAND_FAILURE')
        return ''
    def unit(name):
        if name==migrate.OLD_TIMER:
            return {'ActiveState':'active' if state['timer_active'] else 'inactive',
                    'UnitFileState':'enabled' if state['timer_active'] else 'disabled'}
        return {'ActiveState':'active' if state['daemon_active'] else 'inactive','MainPID':'24'}
    monkeypatch.setattr(migrate,'run',run);monkeypatch.setattr(migrate,'unit_state',unit)
    monkeypatch.setattr(migrate,'container_fingerprint',lambda:before)
    monkeypatch.setattr(migrate,'create_file',lambda p,raw,mode:created.append((p,mode)))
    monkeypatch.setattr(migrate,'await_updates',lambda after:[after+1,after+31])
    return {'calls':calls,'created':created,'before':before,'state':state,
            'plan':{'containers_before':before},'files':{Path(migrate.CODE)/migrate.DAEMON_FILE:b'owned-daemon',migrate.unit_path(migrate.SERVICE):migrate.service_bytes()}}


def test_success_and_repeat_apply_do_not_mutate_business_or_restart_reader(harness):
    for repeat in range(2):
        result=migrate.apply(harness['plan'],harness['files'])
        assert result['state']=='daemon_active_two_snapshots_verified' and result['containers_unchanged']
    assert harness['state']['daemon_active'] and not harness['state']['timer_active']
    assert not harness['state']['reverted']
    assert all(args[0]!='docker' for args in harness['calls'])
    assert [a for a in harness['calls'] if a[:2]==['systemctl','stop']]==[['systemctl','stop',migrate.OLD_SERVICE]]*2
    assert all(p in harness['files'] for p,_ in harness['created'])


@pytest.mark.parametrize('failure',['disable_timer','stop_oneshot','enable_daemon','freshness','container_changed'])
def test_apply_failure_restores_old_timer_without_touching_business(harness,monkeypatch,failure):
    commands={'disable_timer':['systemctl','disable','--now',migrate.OLD_TIMER],
              'stop_oneshot':['systemctl','stop',migrate.OLD_SERVICE],
              'enable_daemon':['systemctl','enable','--now',migrate.SERVICE]}
    if failure in commands:harness['state']['fail']=commands[failure]
    elif failure=='freshness':
        def stale(after):raise ValueError('TWO_NEW_SNAPSHOTS_REQUIRED')
        monkeypatch.setattr(migrate,'await_updates',stale)
    else:monkeypatch.setattr(migrate,'container_fingerprint',lambda:{'owned-business':'changed'})
    result=migrate.apply(harness['plan'],harness['files'])
    assert result['state']=='migration_failed' and result['old_timer_restored']
    assert not result['rollback_health_asserted'] and harness['state']['timer_active']
    assert not harness['state']['daemon_active']
    assert harness['calls'][-2:]==[['systemctl','disable','--now',migrate.SERVICE],['systemctl','enable','--now',migrate.OLD_TIMER]]


def test_failed_automatic_rollback_does_not_claim_timer_restored(harness,monkeypatch):
    def stale(after):raise ValueError('TWO_NEW_SNAPSHOTS_REQUIRED')
    monkeypatch.setattr(migrate,'await_updates',stale)
    harness['state']['fail']=['systemctl','enable','--now',migrate.OLD_TIMER]
    result=migrate.apply(harness['plan'],harness['files'])
    assert result['state']=='migration_failed' and result['old_timer_restored'] is False


def test_invalid_unit_before_cutover_never_disables_old_timer(harness):
    harness['state']['fail']=['systemd-analyze','verify',str(migrate.unit_path(migrate.SERVICE))]
    with pytest.raises(ValueError):migrate.apply(harness['plan'],harness['files'])
    assert harness['state']['timer_active'] and not harness['state']['daemon_active']
    assert not any(a[:2]==['systemctl','disable'] for a in harness['calls'])


def test_manual_rollback_preserves_files_and_marks_known_old_problem(harness):
    harness['state'].update(timer_active=False,daemon_active=True)
    result=migrate.rollback(harness['plan'])
    assert result['state']=='old_timer_restored_health_not_asserted' and result['known_old_wal_limitation_remains']
    assert result['files_deleted']==0 and harness['created']==[]
    assert harness['calls']==[['systemctl','disable','--now',migrate.SERVICE],['systemctl','enable','--now',migrate.OLD_TIMER]]


@pytest.mark.parametrize('case',['two_updates','repeat_timestamp','pid_changed','inactive'])
def test_readiness_requires_two_new_snapshots_from_same_running_process(monkeypatch,case):
    tick=iter([0,1,2,3,4,6]);snapshots=iter([11,11 if case=='repeat_timestamp' else 41])
    units=iter([{'ActiveState':'active','MainPID':'7'},
                {'ActiveState':'inactive' if case=='inactive' else 'active','MainPID':'8' if case=='pid_changed' else '7'}])
    monkeypatch.setattr(migrate.time,'monotonic',lambda:next(tick))
    monkeypatch.setattr(migrate.time,'sleep',lambda value:None)
    monkeypatch.setattr(migrate,'unit_state',lambda name:next(units,{'ActiveState':'active','MainPID':'7'}))
    monkeypatch.setattr(migrate,'fresh_generation',lambda after:next(snapshots,11))
    if case=='two_updates':assert migrate.await_updates(10,timeout=5)==[11,41]
    else:
        with pytest.raises(ValueError):migrate.await_updates(10,timeout=5)


@pytest.mark.parametrize('change',['new','old','future','nan','readonly_false','wrong_version','wrong_mode','list'])
def test_freshness_gate_rejects_old_or_invalid_snapshot(monkeypatch,change):
    import json
    value={'version':'lighthouse-observer-snapshot-v1','read_only':True,'provider_requests':0,'generated_at':100.0}
    if change=='old':value['generated_at']=40
    if change=='future':value['generated_at']=1000
    if change=='nan':value['generated_at']=float('nan')
    if change=='readonly_false':value['read_only']=False
    if change=='wrong_version':value['version']='other'
    if change=='list':value=[]
    monkeypatch.setattr(migrate,'root_file',lambda *a,**k:json.dumps(value).encode())
    monkeypatch.setattr(migrate.time,'time',lambda:101.0)
    monkeypatch.setattr(Path,'stat',lambda *a,**k:SimpleNamespace(st_gid=10001,st_mode=0o100666 if change=='wrong_mode' else 0o100640))
    assert migrate.fresh_generation(50)==(100.0 if change=='new' else None)
