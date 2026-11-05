"""Temporary authenticated loopback GUI; host supervises, Chromium runs UID10001."""
from pathlib import Path
import contextlib,json,os,secrets,signal,subprocess,sys,time
ROOT=Path('/opt/lighthouse-hub');STATE=ROOT/'data/x-login-session/state.json'
NAME='lighthouse-x-login-session';TTL=1200
def command(args,**kw):return subprocess.run(args,check=True,**kw)
def state_write(value):
    temporary=STATE.with_suffix('.partial');temporary.write_text(json.dumps(value),encoding='utf-8');temporary.chmod(0o600);temporary.replace(STATE)
def cleanup():
    if not STATE.exists():return
    value=json.loads(STATE.read_text());
    if value.get('state')=='closed':return
    subprocess.run(['docker','stop','-t','25',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=35)
    subprocess.run(['docker','rm','-f',NAME],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    sys.path.insert(0,str(ROOT));from services.hub.db import Store
    store=Store(ROOT/'data/hub.sqlite3')
    with store.transaction() as db:
        changed=db.execute("SELECT 1 FROM audit WHERE seq>? AND target='tweet' AND action IN ('pause_pipeline','resume_pipeline') LIMIT 1",(value['pause_audit_seq'],)).fetchone()
        if not changed:db.execute("UPDATE pipelines SET paused=? WHERE name='tweet'",(value['prior_paused'],))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),'temporary-login','close_x_login','tweet','kept_external_pause' if changed else 'restored_prior_pause'))
    value.update(state='closed',closed_at=time.time(),profile_preserved=True);state_write(value)
    password=STATE.parent/'password.txt';password.unlink(missing_ok=True)
def gui():
    if os.getuid()!=10001:raise RuntimeError('NONROOT_GUI_REQUIRED')
    for name in ('MODEL_API_KEY','OPENAI_API_KEY','YOUTUBE_API_KEY','HUB_MAINTAIN_TOKEN','HUB_READ_TOKEN'):
        if os.getenv(name):raise RuntimeError('GUI_ENVIRONMENT_CONTAINS_API_SECRET')
    work=Path('/tmp/login-gui');work.mkdir(mode=0o700)
    auth=work/'Xauthority';cookie=secrets.token_hex(16)
    command(['xauth','-f',str(auth),'add',':99','.',cookie],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    env={**os.environ,'DISPLAY':':99','XAUTHORITY':str(auth),'XDG_CONFIG_HOME':'/tmp/login-gui/config','XDG_CACHE_HOME':'/tmp/login-gui/cache'}
    processes=[]
    def stop(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        processes.append(subprocess.Popen(['Xvfb',':99','-screen','0','1280x1000x24','-nolisten','tcp','-auth',str(auth)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        deadline=time.time()+10
        while not Path('/tmp/.X11-unix/X99').exists():
            if time.time()>deadline:raise RuntimeError('GUI_DISPLAY_UNAVAILABLE')
            time.sleep(.1)
        processes.append(subprocess.Popen(['x11vnc','-display',':99','-auth',str(auth),'-listen','127.0.0.1','-rfbport','5901','-localhost','-no6','-forever','-shared','-passwdfile','/run/login-session/password.txt','-nosel'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        processes.append(subprocess.Popen(['/usr/bin/websockify','--web','/usr/share/novnc','127.0.0.1:6081','127.0.0.1:5901'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        browser=subprocess.Popen(['node',str(ROOT/'scripts/login-gui/browser.mjs')],env=env);processes.append(browser)
        end=time.monotonic()+TTL
        while time.monotonic()<end:
            if any(p.poll() is not None for p in processes):break
            time.sleep(.5)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=15)
                except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
def host():
    if os.getuid()!=0:raise RuntimeError('HOST_ORCHESTRATOR_REQUIRES_SUDO')
    sys.path.insert(0,str(ROOT));from services.hub.db import Store
    from services.hub.locks import exclusive
    store=Store(ROOT/'data/hub.sqlite3');STATE.parent.mkdir(mode=0o700,exist_ok=True)
    if STATE.exists() and json.loads(STATE.read_text()).get('state')!='closed':raise RuntimeError('LOGIN_SESSION_ALREADY_ACTIVE')
    with store.transaction() as db:
        prior=db.execute("SELECT paused FROM pipelines WHERE name='tweet'").fetchone()['paused'];db.execute("UPDATE pipelines SET paused=1 WHERE name='tweet'")
        seq=db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),'temporary-login','pause_for_x_login','tweet','owner_requested_manual_server_login')).lastrowid
    value={'state':'waiting','prior_paused':prior,'pause_audit_seq':seq,'started_at':time.time(),'expires_at':time.time()+TTL,'reason':'owner_manual_server_x_login'};state_write(value)
    password=STATE.parent/'password.txt';password.write_text(secrets.token_urlsafe(9)[:8]+'\n');password.chmod(0o600);os.chown(password,10001,10001);os.chmod(STATE.parent,0o711)
    def stop(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    deadline=time.time()+120
    try:
        while store.query("SELECT 1 FROM jobs WHERE queue='tweet' AND state='processing' LIMIT 1"):
            if time.time()>deadline:raise RuntimeError('TWEET_HANDLER_STILL_ACTIVE')
            time.sleep(1)
        stack=contextlib.ExitStack()
        while True:
            try:stack.enter_context(exclusive(ROOT/'data/locks/heavy'));break
            except OSError:
                if time.time()>deadline:raise RuntimeError('HEAVY_BROWSER_STILL_ACTIVE')
                time.sleep(1)
        with stack:
            profile=ROOT/'data/browser-profiles/x'
            if not profile.is_dir() or profile.is_symlink() or profile.stat().st_uid!=10001:raise RuntimeError('X_PROFILE_OWNER_INVALID')
            args=['docker','run','--pull=never','--rm','--name',NAME,'--init','--network','host','--user','10001:10001','--read-only','--cap-drop','ALL',
             '--security-opt','no-new-privileges:true','--security-opt',str('seccomp='+str(ROOT/'config/browser-seccomp.json')),
             '--pids-limit','256','--memory','1400m','--shm-size','256m','--tmpfs','/tmp:rw,size=192m,mode=1777',
             '--env','DISPLAY=:99','--env','PLAYWRIGHT_BROWSERS_PATH='+str(ROOT/'compositor/runtime/browsers'),
             '--mount',f'type=bind,src={profile},dst={profile}',
             '--mount',f'type=bind,src={STATE.parent},dst=/run/login-session,readonly',
             '--mount',f'type=bind,src={ROOT}/scripts/login-gui,dst={ROOT}/scripts/login-gui,readonly',
             '--entrypoint','python','lighthouse-hub:x-login-temp',str(ROOT/'scripts/login-gui/session.py'),'gui']
            value.update(state='active',expires_at=time.time()+TTL);state_write(value)
            process=subprocess.Popen(args)
            try:process.wait(timeout=TTL+30)
            except subprocess.TimeoutExpired:pass
            finally:cleanup()
    finally:cleanup()
if __name__=='__main__':
    {'host':host,'gui':gui,'cleanup':cleanup}[sys.argv[1]]()
