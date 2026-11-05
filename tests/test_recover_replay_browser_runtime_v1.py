from types import SimpleNamespace
import json,subprocess
import pytest
from scripts.ops import recover_replay_browser_runtime_v1 as r


def test_safe_launch_categories_never_include_error_text_or_credentials():
    function=r.PROBE[r.PROBE.index('const classify='):r.PROBE.index('let result;')]
    code=function+"console.log(JSON.stringify(['profile appears to be in use by another Chromium process PRIVATE_COOKIE','Executable doesn\\'t exist PRIVATE_KEY','chrome_crashpad_handler --database is required','No usable sandbox','pthread_create Resource temporarily unavailable','EACCES Permission denied','Owned unknown'].map(message=>classify({message}))));"
    result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==['PROFILE_IN_USE','EXECUTABLE_UNAVAILABLE','CRASHPAD_DATABASE','SANDBOX_UNAVAILABLE','PROCESS_RESOURCE_UNAVAILABLE','FILESYSTEM_UNWRITABLE','UNCLASSIFIED_LAUNCH_ERROR']
    assert 'PRIVATE' not in result.stdout


@pytest.mark.parametrize('bad',['none','wrong_target','extra_cookie_file'])
def test_quarantine_renames_only_three_fixed_ipc_links_and_rejects_other_files(monkeypatch,bad):
    expected={name:{'target':'VM-Owned-22' if name=='SingletonLock' else 'Owned IPC target','inode':index,'device':1,'mtime_ns':0} for index,name in enumerate(r.NAMES)}
    calls=[]
    monkeypatch.setattr(r,'links',lambda fd:expected)
    monkeypatch.setattr(r,'links_one',lambda fd,name:expected[name])
    for name in ('O_DIRECTORY','O_NOFOLLOW'):monkeypatch.setattr(r.os,name,0,raising=False)
    monkeypatch.setattr(r.os,'mkdir',lambda *args,**kwargs:calls.append(('mkdir',args,kwargs)))
    monkeypatch.setattr(r.os,'open',lambda *args,**kwargs:77)
    monkeypatch.setattr(r.os,'rename',lambda *args,**kwargs:calls.append(('rename',args,kwargs)))
    for name in ('fchown','fchmod','fsync','close'):monkeypatch.setattr(r.os,name,lambda *args:None,raising=False)
    target='VM-Owned-22'
    if bad=='wrong_target':target='VM-Different-33'
    elif bad=='extra_cookie_file':expected['Cookies']={'target':'forbidden'}
    if bad!='none':
        with pytest.raises(AssertionError):r.quarantine(10,expected,target)
        assert calls==[]
    else:
        path=r.quarantine(10,expected,target);assert path.startswith('.singleton-stale-quarantine-')
        changed=[item for item in calls if item[0]=='rename']
        assert [item[1] for item in changed]==[(name,name) for name in r.NAMES]
        assert all(item[2]=={'src_dir_fd':10,'dst_dir_fd':77} for item in changed)


def test_owner_scan_matches_process_namespace_inode_and_both_argument_spellings():
    class Entry:
        def __init__(self,folder,parts):self.folder,self.parts=folder,parts
        def __truediv__(self,part):return Entry(self.folder,self.parts+[part])
        def read_bytes(self):
            assert self.parts==['cmdline'];return self.folder.args
        def stat(self):
            assert self.parts[0] in ('root','cwd');return SimpleNamespace(st_dev=self.folder.identity[0],st_ino=self.folder.identity[1])
    class Folder:
        def __init__(self,name,args,identity):self.name,self.args,self.identity=name,args,identity
        def __truediv__(self,part):return Entry(self,[part])
    proc=SimpleNamespace(iterdir=lambda:[Folder('101',b'chrome\x00--user-data-dir=/different/alias\x00PRIVATE_COOKIE\x00',(1,2)),
        Folder('102',b'chrome\x00--user-data-dir\x00/profile\x00',(1,2)),Folder('103',b'chrome\x00--user-data-dir=/other\x00',(9,9))])
    value=r.owners((1,2),proc=proc)
    assert value=={'matching_owner_pids':[101,102],'unreadable_pids':[]}
    assert 'PRIVATE' not in json.dumps(value)


@pytest.mark.parametrize('change',['none','wrong_label','wrong_profile','wrong_image'])
def test_timeout_cleanup_can_stop_only_its_exact_owned_probe(monkeypatch,change):
    identifier='a'*32;name='lighthouse-replay-launch-probe-'+identifier[:12];cid='b'*64;calls=[]
    value={'Id':cid,'Image':r.IMAGE,'Config':{'User':'10001:10001','Labels':{'lighthouse.task':'replay-launch-only-v1','lighthouse.probe-id':identifier}},
        'Mounts':[{'Type':'bind','Source':str(r.PROFILE),'Destination':str(r.PROFILE),'RW':True}],'HostConfig':{'NetworkMode':'none'}}
    if change=='wrong_label':value['Config']['Labels']['lighthouse.probe-id']='c'*32
    elif change=='wrong_profile':value['Mounts'][0]['Source']='/Owned another profile'
    elif change=='wrong_image':value['Image']='sha256:owned-other'
    def command(args,**kwargs):
        calls.append(args)
        if args[1]=='inspect':return json.dumps([value])
        if args[1]=='stop':return cid
        if args[1]=='ps':return ''
        raise AssertionError('Unexpected command')
    monkeypatch.setattr(r,'run',command)
    if change=='none':
        assert r.stop_own_probe(name,identifier)['only_owned_probe_stopped'] is True
        assert [args for args in calls if args[1]=='stop']==[['/usr/bin/docker','stop','--time','15',cid]]
    else:
        with pytest.raises(AssertionError):r.stop_own_probe(name,identifier)
        assert not any(args[1]=='stop' for args in calls)


def test_main_probe_timeout_after_all_environment_arguments_cleans_same_unique_name(monkeypatch):
    identifier='d'*32;calls=[]
    monkeypatch.setattr(r.uuid,'uuid4',lambda:SimpleNamespace(hex=identifier))
    def timeout(command,**kwargs):
        assert command[command.index('--name')+1]=='lighthouse-replay-launch-probe-'+identifier[:12]
        assert all(name+'='+value in command for name,value in r.ENV.items())
        raise r.subprocess.TimeoutExpired(command,90)
    def clean(name,tag):calls.append((name,tag));return {'only_owned_probe_stopped':True}
    monkeypatch.setattr(r.subprocess,'run',timeout);monkeypatch.setattr(r,'stop_own_probe',clean)
    result=r.launch_only('Owned same hostname')
    assert result['runtime_probe_exit_code']==124 and result['probe_cleanup']['only_owned_probe_stopped'] is True
    assert calls==[('lighthouse-replay-launch-probe-'+identifier[:12],identifier)]
