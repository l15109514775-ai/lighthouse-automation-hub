"""Temporary owner-operated loopback desktop. No automation or secret logging."""
from pathlib import Path
import os,secrets,signal,subprocess,time
root=Path('/opt/lighthouse-hub')
assert os.getuid()==10001
assert not any(os.getenv(k) for k in ('MODEL_API_KEY','YOUTUBE_API_KEY','OPENAI_API_KEY','BILIBILI_ACCESS_TOKEN','HUB_MAINTAIN_TOKEN'))
profile=root/'data/browser-profiles/youtube'
assert profile.is_dir() and not profile.is_symlink() and profile.stat().st_uid==10001
tmp=Path('/tmp/youtube-login');tmp.mkdir(mode=0o700)
auth=tmp/'Xauthority';auth.touch(mode=0o600)
subprocess.run(['xauth','-f',str(auth),'add',':98','.',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
env={**os.environ,'HOME':str(tmp),'DISPLAY':':98','XAUTHORITY':str(auth),'XDG_CONFIG_HOME':str(tmp/'config'),'XDG_CACHE_HOME':str(tmp/'cache')}
for name in ('config','cache'):(tmp/name).mkdir(mode=0o700)
processes=[]
def stop(*args):raise SystemExit(0)
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
try:
    processes.append(subprocess.Popen(['Xvfb',':98','-screen','0','1280x1000x24','-nolisten','tcp','-auth',str(auth)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
    deadline=time.monotonic()+10
    while not Path('/tmp/.X11-unix/X98').exists():
        if time.monotonic()>deadline:raise RuntimeError('GUI_DISPLAY_UNAVAILABLE')
        time.sleep(.1)
    vnc_log=(tmp/'vnc-startup.log').open('wb');proxy_log=(tmp/'proxy-startup.log').open('wb')
    processes.append(subprocess.Popen(['x11vnc','-display',':98','-auth',str(auth),'-listen','127.0.0.1','-rfbport','5902',
        '-localhost','-no6','-forever','-shared','-passwdfile','/run/youtube-login/password.txt','-nosel'],env=env,stdout=subprocess.DEVNULL,stderr=vnc_log))
    processes.append(subprocess.Popen(['/usr/bin/websockify','--web','/usr/share/novnc','127.0.0.1:6082','127.0.0.1:5902'],env=env,stdout=subprocess.DEVNULL,stderr=proxy_log))
    executable=subprocess.check_output(['node','--input-type=module','-e',
        "import {chromium} from './compositor/node_modules/playwright/index.mjs';process.stdout.write(chromium.executablePath())"],cwd=root,env=env,text=True)
    assert Path(executable).resolve().is_relative_to(root/'compositor/runtime/browsers') and Path(executable).is_file()
    startup=tmp/'browser-startup.log'
    log=startup.open('wb')
    processes.append(subprocess.Popen([executable,'--user-data-dir='+str(profile),'--no-first-run','--no-default-browser-check',
        '--window-size=1280,1000','--start-maximized','https://www.youtube.com/'],env=env,stdout=subprocess.DEVNULL,stderr=log))
    for _ in range(20):
        if processes[-1].poll() is not None:
            log.flush()
            import re
            print(re.sub(r'https?://\S+','[redacted-url]',startup.read_text(errors='replace')[:2000]),flush=True)
            raise RuntimeError('YOUTUBE_GUI_BROWSER_START_FAILED')
        time.sleep(.25)
    log.close()
    if any(p.poll() is not None for p in processes):
        vnc_log.flush();proxy_log.flush()
        print({'startup_exit_codes':[p.poll() for p in processes]},flush=True)
        for name in ('vnc-startup.log','proxy-startup.log'):
            print(name+': '+(tmp/name).read_text(errors='replace')[-2000:],flush=True)
        raise RuntimeError('YOUTUBE_GUI_COMPONENT_START_FAILED')
    for name in ('browser-startup.log','vnc-startup.log','proxy-startup.log'):(tmp/name).unlink()
    print('YOUTUBE_OWNER_BROWSER_READY',flush=True)
    expiry=time.monotonic()+1800
    while time.monotonic()<expiry and all(p.poll() is None for p in processes):time.sleep(.5)
finally:
    for p in reversed(processes):
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
