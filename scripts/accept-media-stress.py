"""Owned >4GB/5h stream-copy acceptance; isolated Linux container, no provider access."""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import threading
import time

from adapters.media_tools import inspect_media, process_lines
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.evidence import sha256_file
from services.hub.errors import HubError
from services.hub.locks import exclusive
from services.hub.maintenance import Maintenance
from services.hub.media_processing import MediaProcessing, atomic_json


def cgroup(name):
    file=Path('/sys/fs/cgroup')/name
    text=file.read_text().strip()
    return int(text) if text.isdigit() else {line.split()[0]:int(line.split()[1]) for line in text.splitlines()}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    args=parser.parse_args()
    root=Path(args.root).resolve()
    assert root.is_relative_to(Path('/acceptance')) and not root.exists()
    assert os.getuid()==10001
    assert cgroup('memory.max')==536870912
    cfg=deepcopy(Settings.load().config)
    settings=Settings(root,cfg,True);settings.prepare()
    store=Store(settings.db_path);store.init()
    maintenance=Maintenance(settings,store)
    start=time.monotonic()
    disk_start=maintenance.check_disk(reserve_bytes=12_000_000_000)
    state={'phase':'initializing','elapsed_seconds':0,'minimum_disk_free_bytes':disk_start,
           'maximum_cgroup_memory_current_bytes':0,'heartbeat_failed':False}
    stop=threading.Event()
    job=None

    def monitor():
        reported=0
        while not stop.wait(2):
            state['elapsed_seconds']=round(time.monotonic()-start,2)
            state['minimum_disk_free_bytes']=min(state['minimum_disk_free_bytes'],shutil.disk_usage(root).free)
            state['maximum_cgroup_memory_current_bytes']=max(state['maximum_cgroup_memory_current_bytes'],cgroup('memory.current'))
            if job and not store.heartbeat(job,180):state['heartbeat_failed']=True
            atomic_json(root.parent/'progress.json',state)
            if time.monotonic()-reported>=15:
                print(json.dumps({'progress':state}),flush=True);reported=time.monotonic()

    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    try:
        with exclusive(root/'data/locks/video'),exclusive(root/'data/locks/heavy'):
            ident=store.enqueue('owned_synthetic_acceptance','five-hour-over-four-gb',
                                'owned_media_stress','video',{},cfg['policy_version'])
            job=store.claim('video',180,4)
            assert job and job['job_id']==ident
            folder=root/'media'/ident;folder.mkdir()
            seed=folder/'owned-six-second-seed.mp4'
            source=folder/'original.mp4'
            seed_command=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=30',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6','-c:v','libx264','-preset','veryfast',
                '-threads','1','-b:v','2100k','-minrate','2100k','-maxrate','2100k','-bufsize','4200k',
                '-x264-params','nal-hrd=cbr:force-cfr=1','-g','30','-keyint_min','30','-sc_threshold','0','-bf','2',
                '-pix_fmt','yuv420p','-c:a','aac','-b:a','96k','-movflags','+faststart',str(seed)]
            loop_command=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-stream_loop','2999',
                '-i',str(seed),'-t','18000','-map','0:v:0','-map','0:a:0','-c','copy','-threads','1',
                '-avoid_negative_ts','make_zero','-movflags','+faststart',str(source)]
            def generation_guard():
                maintenance.check_disk()
                if source.exists() and source.stat().st_size>6_000_000_000:
                    raise HubError('OWNED_FIXTURE_SIZE_LIMIT')
            state['phase']='generate_owned_seed'
            for _ in process_lines(seed_command,folder,60,generation_guard):pass
            state['phase']='stream_copy_five_hour_source'
            for _ in process_lines(loop_command,folder,1800,generation_guard):pass
            info=inspect_media(source,check=maintenance.check_disk)
            assert 4_000_000_000<=info['bytes']<=6_000_000_000
            assert 17999<=info['duration']<=18001
            state['phase']='hash_source'
            asset={'file':str(source),'sha256':sha256_file(source),'bytes':info['bytes']}
            store.checkpoint(job,'synthetic_media_acquired',{'source_asset':asset})
            state['phase']='verify_and_segment_full_size_source'
            processed_started=time.monotonic()
            result=MediaProcessing(settings,store).run(job,asset)
            processed_elapsed=time.monotonic()-processed_started
            assert result['packet_coverage_verified'] and len(result['outputs'])>=2
            assert all(out['duration']<=17400 and out['bytes']<=3800000000 for out in result['outputs'])
            state['phase']='reopen_database_and_reuse_completed_batch'
            reopened=Store(settings.db_path);reopened.init()
            reused=MediaProcessing(settings,reopened).run(reopened.get(ident),asset)
            assert reused==result and sha256_file(source)==asset['sha256']
            assert not state['heartbeat_failed']
            assert all(reopened.query('SELECT COUNT(*) n FROM '+table)[0]['n']==0
                       for table in ('publications','publication_reviews','video_review_decisions','api_calls'))
            manifest=json.loads(Path(result['manifest']).read_text())
            assert manifest['human_review'] is False and manifest['publishable'] is False
            assert state['minimum_disk_free_bytes']>=15_000_000_000
            events=cgroup('memory.events')
            assert events.get('oom',0)==events.get('oom_kill',0)==0
            peak=cgroup('memory.peak')
            assert peak<=536870912
            state['phase']='completed'
            report={'acceptance':'owned-media-stress-v1','success':True,
                'runner_sha256':sha256_file(Path(__file__)),
                'runtime_commit':json.loads(Path('/opt/lighthouse-hub/source-version.json').read_text())['commit'],
                'uid':os.getuid(),'network':'none','readonly_rootfs':True,'cpu_limit':1,
                'memory_limit_bytes':536870912,'cgroup_memory_peak_bytes':peak,'cgroup_memory_events':events,
                'child_peak_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                'source_bytes':info['bytes'],'source_duration_seconds':info['duration'],'source_sha256':asset['sha256'],
                'source_preserved':True,'seed_sha256':sha256_file(seed),'seed_bytes':seed.stat().st_size,
                'seed_duration_seconds':inspect_media(seed)['duration'],
                'recipe':{'seed_command':seed_command,'loop_command':loop_command},
                'ffmpeg':subprocess.check_output(['ffmpeg','-version'],text=True).splitlines()[0],
                'processing_elapsed_seconds':round(processed_elapsed,2),'total_elapsed_seconds':round(time.monotonic()-start,2),
                'disk_start_free_bytes':disk_start,'minimum_disk_free_bytes':state['minimum_disk_free_bytes'],
                'disk_end_free_bytes':maintenance.check_disk(),'disk_floor_bytes':15_000_000_000,
                'packet_payload_chains_verified':True,'source_packets':result['source_packets'],
                'render_attempts':result['render_attempts'],'parts':len(result['outputs']),
                'part_durations':[out['duration'] for out in result['outputs']],
                'part_bytes':[out['bytes'] for out in result['outputs']],
                'part_sha256':[out['sha256'] for out in result['outputs']],
                'completed_checkpoint_reused':True,'human_approvals':0,'external_api_calls':0,'external_publications':0,
                'fixture_root':str(root),'job_id':ident,'manifest':result['manifest']}
            atomic_json(root.parent/'result.json',report)
            store.finish(job,'completed',result={**job['result'],'acceptance':report},stage='owned_stress_done')
            print(json.dumps({'result':report}),flush=True)
    except BaseException as exc:
        atomic_json(root.parent/'failure.json',{'phase':state['phase'],
            'error_code':exc.code if isinstance(exc,HubError) else type(exc).__name__,
            'elapsed_seconds':round(time.monotonic()-start,2),'success':False,
            'originals_and_checkpoints_preserved':True})
        raise
    finally:
        stop.set();thread.join(timeout=3)


if __name__=='__main__':main()
