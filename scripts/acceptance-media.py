"""Synthetic offline Linux media/renderer test; never publishes or claims human review."""
from pathlib import Path
import json
import subprocess
from adapters.compositor import Compositor, compositor_input
from adapters.video import VideoProcessor
from services.hub.evidence import sha256_file

root = Path('/opt/lighthouse-hub')
folder = root / 'media/synthetic-acceptance'
folder.mkdir(parents=True, exist_ok=True)
source = {'post':{'id':'1000000000000000020','text':'テスト20:00\n配信します','created_at':'2026-10-30T12:00:00Z'},
          'author':{'name':'SYNTHETIC TEST ONLY','username':'fixture_only'},
          'url':'https://x.com/fixture_only/status/1000000000000000020'}
render = Compositor(root).render(compositor_input(source,'测试20:00\n将进行直播'),folder)
assert render['report']['publishable'] is False
assert render['report']['engine']['platform'] == 'linux'
source_file = folder / 'synthetic-original.mp4'
subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','2','-c:v','libx264','-threads','1',
                '-pix_fmt','yuv420p','-c:a','aac',str(source_file)],check=True,timeout=30)
processed = VideoProcessor(root).remux(source_file,folder / 'synthetic-processed.mp4',lambda reserve=0:None)
assert processed['duration'] > 1 and processed['sha256'] == sha256_file(Path(processed['file']))
print(json.dumps({'synthetic_only':True,'linux_renderer':True,'nonroot_uid':__import__('os').getuid(),
                  'png_sha256':render['sha256'],'publishable':False,'ffmpeg_remux':processed},indent=2))
