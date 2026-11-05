"""Private immutable extractor observations, not a passed-flag contract.

Actual signed media URLs can occur in raw extractor info; files stay0600 in
the job's0700 metadata workspace. Authentication/header credentials are
rejected. No raw value is printed or placed in a task error message.
"""
from datetime import datetime,timezone
import hashlib,json,math,os,re,stat,uuid
from pathlib import Path

from services.hub.db import encode
from services.hub.errors import HubError

VERSION='youtube-readiness-extractor-observation-v3'
LIMIT=2_097_152
FORBIDDEN=re.compile(r'^(?:cookie|cookies|authorization|proxy.authorization|password|passwd|secret(?:id|key)?|api.key|access.token|refresh.token|bearer.token)$',re.I)
NORMALIZATION_VERSION='youtube-readiness-runtime-normalization-v1'
ALIAS_NORMALIZATION_VERSION='youtube-readiness-runtime-normalization-v2'
FORMAT_IDENTITY_FIELDS=('format_id','ext','protocol','vcodec','acodec','width','height','fps','filesize',
    'filesize_approx','tbr','abr','asr','audio_channels','dynamic_range')
FRAGMENT_MARKER={'_type':'youtube-unexpanded-runtime-fragments-v1',
    'python_type':'yt_dlp.utils.LazyList','expanded':False}


def guard(value,depth=0):
    if depth>40:raise HubError('YOUTUBE_READINESS_RAW_TOO_DEEP')
    if isinstance(value,dict):
        if any(not isinstance(k,str) or FORBIDDEN.fullmatch(k) for k in value):raise HubError('YOUTUBE_READINESS_RAW_CONTAINS_CREDENTIAL')
        for item in value.values():guard(item,depth+1)
    elif isinstance(value,(list,tuple)):
        for item in value:guard(item,depth+1)
    elif value is not None and type(value) not in (str,int,float,bool):raise HubError('YOUTUBE_READINESS_RAW_NOT_JSON')
    elif type(value) is float and not math.isfinite(value):raise HubError('YOUTUBE_READINESS_RAW_NOT_JSON')


def _format_identity(value):
    if not isinstance(value,dict) or type(value.get('format_id')) is not str:raise ValueError()
    result={}
    for key in FORMAT_IDENTITY_FIELDS:
        item=value.get(key)
        if item is not None and type(item) not in (str,int,float,bool) or type(item) is float and not math.isfinite(item):raise ValueError()
        result[key]={'present':key in value,'value':item}
    return hashlib.sha256(encode(result).encode()).hexdigest()


def _alias_source(info,index):
    """Resolve a requested format by unique ID and exact quality scalars only."""
    requested=info.get('requested_formats');formats=info.get('formats')
    if type(requested) not in (list,tuple) or type(formats) not in (list,tuple) or not 0<=index<len(requested):raise ValueError()
    item=requested[index];identity=_format_identity(item)
    matches=[i for i,source in enumerate(formats) if isinstance(source,dict) and
        type(source.get('format_id')) is str and source['format_id']==item['format_id']]
    if len(matches)!=1 or _format_identity(formats[matches[0]])!=identity:raise ValueError()
    return matches[0],identity


def runtime_snapshot(info):
    """V4 opt-in: describe native lazy fragments without evaluating them.

    yt-dlp can supply LazyList fragments even on an unselected format. Iterating
    or sanitizing those may read the network. A nonempty JSON descriptor keeps
    that format fragmented; it never makes it eligible as an exact direct track.
    V3's guard/record defaults and every other unsupported type stay unchanged.
    """
    from yt_dlp.utils import LazyList
    if not isinstance(info,dict):raise HubError('YOUTUBE_READINESS_RAW_NOT_JSON')
    values=[];aliases=[]
    def walk(value,path,depth):
        if depth>40:raise HubError('YOUTUBE_READINESS_RAW_TOO_DEEP')
        if isinstance(value,dict):
            if any(not isinstance(k,str) or FORBIDDEN.fullmatch(k) for k in value):
                raise HubError('YOUTUBE_READINESS_RAW_CONTAINS_CREDENTIAL')
            return {k:walk(v,(*path,k),depth+1) for k,v in value.items()}
        if isinstance(value,(list,tuple)):
            return [walk(v,(*path,i),depth+1) for i,v in enumerate(value)]
        if (type(value) is LazyList and len(path)==3 and path[0]=='formats' and
                type(path[1]) is int and path[2]=='fragments'):
            values.append({'path':list(path),'python_type':'yt_dlp.utils.LazyList',
                'action':'opaque-fragment-presence','expanded':False})
            return dict(FRAGMENT_MARKER)
        if (type(value) is LazyList and len(path)==3 and path[0]=='requested_formats' and
                type(path[1]) is int and path[2]=='fragments'):
            try:
                index,identity=_alias_source(info,path[1])
                # The actual extractor aliases this object; matching IDs alone
                # cannot make an unrelated lazy source eligible for conversion.
                if value is not info['formats'][index].get('fragments'):raise ValueError()
                aliases.append({'path':list(path),'python_type':'yt_dlp.utils.LazyList',
                    'action':'opaque-fragment-alias','expanded':False,'source_path':['formats',index,'fragments'],
                    'format_identity_sha256':identity})
                return dict(FRAGMENT_MARKER)
            except (ValueError,KeyError,TypeError):raise HubError('YOUTUBE_READINESS_RAW_NOT_JSON') from None
        guard(value,depth)
        return value
    snapshot=walk(info,(),0)
    normalization=({'version':ALIAS_NORMALIZATION_VERSION if aliases else NORMALIZATION_VERSION,
        'values':values+aliases} if values or aliases else None)
    validate_runtime_snapshot(snapshot,normalization)
    return snapshot,normalization


def validate_runtime_snapshot(info,normalization=None):
    """Check the explicit opaque markers when reopening V4 evidence."""
    guard(info)
    if not isinstance(info,dict):raise HubError('YOUTUBE_READINESS_RAW_NOT_JSON')
    markers=[]
    def find(value,path=()):
        if isinstance(value,dict):
            if value.get('_type')==FRAGMENT_MARKER['_type']:
                if value!=FRAGMENT_MARKER or value.get('expanded') is not False:raise ValueError()
                markers.append(path)
            else:
                for key,item in value.items():find(item,(*path,key))
        elif isinstance(value,(list,tuple)):
            for index,item in enumerate(value):find(item,(*path,index))
    try:
        find(info)
        if normalization is None:
            if markers:raise ValueError()
            return
        if (not isinstance(normalization,dict) or set(normalization)!={'version','values'} or
                normalization['version'] not in (NORMALIZATION_VERSION,ALIAS_NORMALIZATION_VERSION) or
                not isinstance(normalization['values'],list) or not 1<=len(normalization['values'])<=600):raise ValueError()
        expected=[]
        for item in normalization['values']:
            alias=isinstance(item,dict) and item.get('action')=='opaque-fragment-alias'
            keys={'path','python_type','action','expanded'}|({'source_path','format_identity_sha256'} if alias else set())
            if (not isinstance(item,dict) or set(item)!=keys or
                    item['python_type']!='yt_dlp.utils.LazyList' or item['action'] not in ('opaque-fragment-presence','opaque-fragment-alias') or
                    item['expanded'] is not False or not isinstance(item['path'],list) or len(item['path'])!=3):raise ValueError()
            root,index,key=item['path']
            if type(index) is not int or index<0 or key!='fragments':raise ValueError()
            if alias:
                if normalization['version']!=ALIAS_NORMALIZATION_VERSION or root!='requested_formats':raise ValueError()
                source_index,identity=_alias_source(info,index)
                if (type(item['source_path']) is not list or len(item['source_path'])!=3 or
                        type(item['source_path'][1]) is not int or
                        item['source_path']!=['formats',source_index,'fragments'] or item['format_identity_sha256']!=identity or
                        info['formats'][source_index].get('fragments')!=FRAGMENT_MARKER):raise ValueError()
            elif root!='formats':raise ValueError()
            expected.append(tuple(item['path']))
        if normalization['version']==NORMALIZATION_VERSION and len(expected)>300:raise ValueError()
        if normalization['version']==ALIAS_NORMALIZATION_VERSION and not any(x['action']=='opaque-fragment-alias' for x in normalization['values']):raise ValueError()
        if len(set(expected))!=len(expected) or set(markers)!=set(expected):raise ValueError()
    except (ValueError,TypeError,KeyError):raise HubError('YOUTUBE_READINESS_NORMALIZATION_INVALID') from None


def record(info,binding,area,http):
    guard(info)
    body={'version':VERSION,'job_id':binding['job_id'],'binding_sha256':hashlib.sha256(encode(binding).encode()).hexdigest(),
        'recorded_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'extractor_info':info,
        'http':dict(http),'media_downloaded':False,'human_review':False,'publication_authorized':False}
    raw=encode(body).encode()
    if len(raw)>LIMIT:raise HubError('YOUTUBE_READINESS_RAW_SIZE_LIMIT')
    file=area/('extractor-observation-'+str(uuid.uuid4())+'.json')
    fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
        if os.name=='posix':
            handle=os.open(area,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(handle)
            finally:os.close(handle)
    except BaseException:
        # An immutable diagnostic may be partial after a process crash. It is
        # never reused without its exact expected hash and byte count.
        raise
    return {'file':str(file),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'version':VERSION}


def read(settings,job,binding,reference):
    try:
        if not isinstance(reference,dict) or set(reference)!={'file','sha256','bytes','version'} or reference['version']!=VERSION:raise ValueError()
        file=Path(reference['file']);area=settings.root/'media'/job['job_id']/'youtube-highest-v3-plan'
        if (file.parent!=area or not re.fullmatch(r'extractor-observation-[a-f0-9-]{36}\.json',file.name) or
                file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or
                type(reference['bytes']) is not int or not 0<reference['bytes']<=LIMIT):raise ValueError()
        fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size!=reference['bytes'] or os.name=='posix' and before.st_mode&0o077:raise ValueError()
            raw=source.read(LIMIT+1);after=os.fstat(source.fileno())
            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError()
        if len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:raise ValueError()
        body=json.loads(raw);guard(body['extractor_info'])
        if (body.get('version')!=VERSION or body.get('job_id')!=job['job_id'] or
                body.get('binding_sha256')!=hashlib.sha256(encode(binding).encode()).hexdigest() or
                any(body.get(k) is not False for k in ('media_downloaded','human_review','publication_authorized')) or
                type(body['http']['requests']) is not int or not 1<=body['http']['requests']<=64):raise ValueError()
        return body
    except (OSError,ValueError,TypeError,KeyError,AttributeError):raise HubError('YOUTUBE_READINESS_EVIDENCE_INVALID') from None
