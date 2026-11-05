from pathlib import Path
import os
import subprocess
import pytest
from adapters.video import VideoProcessor
from services.hub.evidence import sha256_file


@pytest.mark.skipif(os.name=='nt',reason='Existing upstream worker probe requires Linux fcntl; Ubuntu run is mandatory')
def test_existing_worker_probe_and_real_ffmpeg_remux(tmp_path):
    source=tmp_path / 'synthetic.mp4'
    subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','1','-c:v','libx264','-threads','1',
                    '-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True,timeout=15)
    root=Path(__file__).resolve().parents[1]
    result=VideoProcessor(root).remux(source,tmp_path / 'remux.mp4',lambda reserve=0:None)
    assert .5 < result['duration'] < 2
    assert result['sha256'] == sha256_file(Path(result['file']))
