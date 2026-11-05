"""Durable, bounded stream-copy preparation. This module never uploads or approves."""
import csv
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from adapters.media_tools import inspect_media,packet_evidence,empty_evidence,split_media
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .maintenance import Maintenance

VERSION='media-processing-v1'


def sync_directory(folder):
    if os.name!='nt':
        descriptor=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def atomic_json(file,value):
    temporary=file.with_suffix('.partial.json')
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
        raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
    with temporary.open('w',encoding='utf-8') as stream:
        stream.write(encode(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,file)
    sync_directory(file.parent)


def balanced_segment_seconds(duration,bytes_,recipe):
    """Choose the fewest roughly equal stream-copy segments that fit both caps."""
    limit=min(recipe['max_part_seconds']*.97,duration*recipe['max_part_bytes']/max(1,bytes_*1.1))
    count=max(1,math.ceil(duration/max(.1,limit)))
    return count,max(.1,duration/count)


class MediaProcessing:
    VERSION=VERSION
    RESULT_KEY='media_processing'
    DIRECTORY_NAME='parts-v1'
    MAX_SOURCE_SECONDS=86400
    def __init__(self,settings,store):
        self.settings,self.store=settings,store
        self.maintenance=Maintenance(settings,store)

    def _guard(self):pass
    def _sha(self,file):return sha256_file(file)
    def _inspect(self,file,**kwargs):return inspect_media(file,**kwargs)
    def _packets(self,file,streams,**kwargs):return packet_evidence(file,streams,**kwargs)

    def recipe(self):
        cfg=self.settings.config
        recipe={'version':self.VERSION,'max_part_seconds':cfg.get('video_part_max_seconds',36000),
            'max_part_bytes':cfg.get('video_part_max_bytes',14_000_000_000),'max_parts':cfg.get('video_max_parts',32),
            'max_render_attempts':3,'timeout_seconds':cfg.get('video_processing_timeout_seconds',7200)}
        for key,minimum,maximum in (('max_part_seconds',1,36000),('max_part_bytes',16384,14000000000),
                                    ('max_parts',1,32),('timeout_seconds',30,28800)):
            if type(recipe[key]) is not int or not minimum<=recipe[key]<=maximum:
                raise HubError('MEDIA_PROCESSING_LIMITS_INVALID',review=True)
        return recipe

    def _save(self,job,plan):
        self._guard()
        self.store.checkpoint(job,self.RESULT_KEY,{**job['result'],self.RESULT_KEY:deepcopy(plan)})

    def _source(self,job,info):
        file=Path(info['file'])
        root=(self.settings.root/'media'/job['job_id']).resolve()
        if (file.is_symlink() or not root.is_relative_to((self.settings.root/'media').resolve()) or
            not file.resolve().is_relative_to(root) or not file.is_file()):
            raise HubError('MEDIA_SOURCE_PATH_INVALID',review=True)
        if file.stat().st_size>self.settings.config['max_media_bytes'] or self._sha(file)!=info['sha256']:
            raise HubError('MEDIA_SOURCE_INTEGRITY_CHANGED',review=True)
        return file.resolve()

    def _directory(self,job,source,plan,new):
        folder=source.parent/self.DIRECTORY_NAME
        owner={'version':self.VERSION,'job_id':job['job_id'],'source_sha256':plan['source_sha256']}
        if folder.is_symlink(): raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
        if new and folder.exists(): raise HubError('MEDIA_OUTPUT_DIRECTORY_EXISTS',review=True)
        folder.mkdir(exist_ok=True)
        marker=folder/'owner.json'
        if marker.is_symlink(): raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
        if marker.exists():
            try: same=json.loads(marker.read_text('utf-8'))==owner
            except (ValueError,OSError): same=False
            if not same: raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
        else:
            existing=list(folder.iterdir())
            if existing:
                unfinished=folder/'owner.partial.json'
                try: recoverable=existing==[unfinished] and not unfinished.is_symlink() and json.loads(unfinished.read_text('utf-8'))==owner
                except (OSError,ValueError): recoverable=False
                if not recoverable: raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
            atomic_json(marker,owner)
        return folder

    def _clean_candidate(self,source,folder,expected):
        # Only our derived partials; they are reproducible from this verified original.
        if self._sha(source)!=expected: raise HubError('MEDIA_SOURCE_INTEGRITY_CHANGED',review=True)
        files=list(folder.iterdir())
        for file in files:
            if file.name=='owner.json': continue
            if file.is_symlink() or not file.is_file() or not (file.name=='segments.csv' or
                re.fullmatch(r'part-[0-9]{3}\.partial\.mp4',file.name)):
                raise HubError('MEDIA_WORKSPACE_REQUIRES_REVIEW',review=True)
        for file in files:
            if file.name!='owner.json': file.unlink()

    def _watch(self,folder,budget,max_parts):
        self._guard()
        files=list(folder.glob('part-*.partial.mp4'))
        if len(files)>max_parts: raise HubError('MEDIA_PART_COUNT_LIMIT',review=True)
        if any(file.is_symlink() for file in files): raise HubError('MEDIA_WORKSPACE_INVALID',review=True)
        used=sum(file.stat().st_size for file in files)
        if used>budget: raise HubError('MEDIA_OUTPUT_SIZE_BOUND_EXCEEDED',review=True)
        self.maintenance.check_disk(min(1_000_000_000,max(0,budget-used)))

    def _rows(self,folder,duration,maximum):
        file=folder/'segments.csv'
        if not file.is_file() or file.is_symlink() or file.stat().st_size>65536:
            raise HubError('MEDIA_SEGMENT_LIST_INVALID',review=True)
        rows=[]
        try:
            for index,row in enumerate(csv.reader(file.read_text('utf-8').splitlines())):
                if len(row)!=3 or row[0]!=f'part-{index:03d}.partial.mp4' or index>=maximum: raise ValueError()
                start,end=map(float,row[1:])
                previous=rows[-1]['mux_end'] if rows else 0
                if not all(math.isfinite(n) for n in (start,end)) or start<0 or end<=start or abs(start-previous)>.01:
                    raise ValueError()
                rows.append({'index':index,'candidate':row[0],'mux_start':start,'mux_end':end})
            # B-frame timestamp normalization may add a small leading offset, never hide a missing segment.
            if not rows or not -.1<=rows[-1]['mux_end']-duration<=2:
                raise ValueError()
            names={row['candidate'] for row in rows}
            if names!={file.name for file in folder.glob('part-*.partial.mp4')}: raise ValueError()
        except (ValueError,TypeError,OSError):
            raise HubError('MEDIA_SEGMENT_COVERAGE_INVALID',review=True) from None
        return rows

    def run(self,job,info):
        self._guard()
        source=self._source(job,info)
        recipe=self.recipe()
        fingerprint=hashlib.sha256(encode(recipe).encode()).hexdigest()
        plan=deepcopy(job['result'].get(self.RESULT_KEY))
        new=plan is None
        if plan is not None and (plan.get('version')!=self.VERSION or plan.get('source_sha256')!=info['sha256'] or
                                  plan.get('recipe_sha256')!=fingerprint):
            raise HubError('MEDIA_PROCESSING_INPUT_CHANGED',review=True)
        if plan is None:
            details=self._inspect(source)
            if details['duration']>self.MAX_SOURCE_SECONDS: raise HubError('MEDIA_SOURCE_DURATION_LIMIT',review=True)
            proof=self._packets(source,details['stream_types'],timeout=recipe['timeout_seconds'])
            count,seconds=balanced_segment_seconds(details['duration'],details['bytes'],recipe)
            plan={'version':self.VERSION,'recipe_sha256':fingerprint,'recipe':recipe,'source_sha256':info['sha256'],
                'input':details,'source_packets':proof,'state':'building','attempts':0,
                'segment_seconds':max(.1,seconds),'parts':[],'history':[]}
            self._save(job,plan)
        folder=self._directory(job,source,plan,new)
        budget=plan['input']['bytes']+64_000_000*recipe['max_parts']
        while plan['state'] not in ('finalizing','completed'):
            if plan['state']!='verifying':
                if plan['attempts']>=recipe['max_render_attempts']:
                    raise HubError('MEDIA_SEGMENTATION_REQUIRES_TRANSCODING_OR_REVIEW',review=True)
                self._clean_candidate(source,folder,info['sha256'])
                self.maintenance.check_disk(budget)
                plan.update(state='building',attempts=plan['attempts']+1,parts=[])
                self._save(job,plan)
                split_media(source,folder,plan['segment_seconds'],timeout=recipe['timeout_seconds'],
                    check=lambda:self._watch(folder,budget,recipe['max_parts']))
                for file in (*folder.glob('part-*.partial.mp4'),folder/'segments.csv'):
                    with file.open('r+b') as stream: os.fsync(stream.fileno())
                sync_directory(folder)
                plan.update(state='verifying',rows=self._rows(folder,plan['input']['duration'],recipe['max_parts']))
                self._save(job,plan)
            combined=empty_evidence()
            oversized=False
            for row in plan['rows']:
                index=row['index'];file=folder/row['candidate']
                if file.is_symlink() or not file.is_file(): raise HubError('MEDIA_DERIVED_FILE_MISSING',review=True)
                if index<len(plan['parts']):
                    saved=plan['parts'][index]
                    if saved['sha256']!=self._sha(file) or saved['packet_before']!=combined:
                        raise HubError('MEDIA_DERIVED_INTEGRITY_CHANGED',review=True)
                    combined=deepcopy(saved['packet_after']);continue
                details=self._inspect(file,check=self.maintenance.check_disk)
                if details['stream_config']!=plan['input']['stream_config']:
                    raise HubError('MEDIA_CODEC_CONFIGURATION_CHANGED',review=True)
                if details['bytes']>recipe['max_part_bytes'] or details['duration']>recipe['max_part_seconds']:
                    oversized=True;break
                actual=self._packets(file,details['stream_types'],initial=combined,
                    timeout=recipe['timeout_seconds'],check=self.maintenance.check_disk)
                with file.open('r+b') as stream: os.fsync(stream.fileno())
                sync_directory(folder)
                plan['parts'].append({**row,'sha256':self._sha(file),'bytes':details['bytes'],'duration':details['duration'],
                    'packet_before':deepcopy(combined),'packet_after':actual})
                combined=deepcopy(actual)
                self._save(job,plan)
            if oversized:
                plan['history'].append({'attempt':plan['attempts'],'code':'MEDIA_PART_LIMIT_EXCEEDED',
                                        'segment_seconds':plan['segment_seconds']})
                plan.update(state='building',segment_seconds=plan['segment_seconds']/2,parts=[])
                self._save(job,plan)
                continue
            if combined!=plan['source_packets']:
                raise HubError('MEDIA_PACKET_COVERAGE_MISMATCH',review=True)
            if self._sha(source)!=info['sha256']: raise HubError('MEDIA_SOURCE_INTEGRITY_CHANGED',review=True)
            plan['state']='finalizing';self._save(job,plan)
        outputs=[]
        for part in plan['parts']:
            staged=folder/part['candidate'];final=folder/f"part-{part['index']:03d}.mp4"
            selected=final if final.exists() else staged
            if selected.is_symlink() or not selected.is_file() or self._sha(selected)!=part['sha256']:
                raise HubError('MEDIA_DERIVED_INTEGRITY_CHANGED',review=True)
            if not final.exists(): os.replace(staged,final)
            outputs.append({'file':str(final),'sha256':part['sha256'],'duration':part['duration'],
                            'bytes':part['bytes'],'processing':self.VERSION})
        sync_directory(folder)
        plan['state']='completed';self._save(job,plan)
        manifest={'version':self.VERSION,'source_sha256':info['sha256'],'recipe':recipe,'parts':plan['parts'],
                  'source_packets':plan['source_packets'],'packet_coverage_verified':True,'outputs':outputs,
                  'human_review':False,'publishable':False}
        document=folder/'manifest.json'
        atomic_json(document,manifest)
        return {'outputs':outputs,'manifest':str(document),'packet_coverage_verified':True,
                'source_packets':plan['source_packets'],'render_attempts':plan['attempts']}
