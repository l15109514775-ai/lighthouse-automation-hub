"""Bounded public CDN rendition derivation, not original-upload identity."""
import re
from urllib.parse import parse_qsl,urlsplit
from services.hub.errors import HubError

RETRIEVAL_RULE='x-pbs-api-photo-orig-v1'


def official_photo_retrieval(source_url):
    """Keep the receipt URL intact; derive exactly one same-media orig rendition."""
    try:
        if not isinstance(source_url,str) or any(ord(c)<=32 or ord(c)==127 for c in source_url):raise ValueError()
        parsed=urlsplit(source_url)
        if (parsed.scheme!='https' or parsed.netloc!='pbs.twimg.com' or parsed.fragment or
                source_url.endswith(('#','?'))):raise ValueError()
        match=re.fullmatch(r'/media/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?',parsed.path)
        if not match:raise ValueError()
        pairs=parse_qsl(parsed.query,strict_parsing=True,keep_blank_values=True)
        params=dict(pairs)
        if len(params)!=len(pairs) or not set(params)<={'format','name'} or any(not value for _,value in pairs):raise ValueError()
        extension=match[2].replace('jpeg','jpg') if match[2] else None
        requested=params.get('format')
        if requested is not None:
            requested=requested.replace('jpeg','jpg')
            if requested not in ('jpg','png','webp') or extension is not None and extension!=requested:raise ValueError()
        fmt=requested or extension
        if fmt not in ('jpg','png','webp'):raise ValueError()
        return {'download_url':f'https://pbs.twimg.com/media/{match[1]}?format={fmt}&name=orig',
                'retrieval_rule':RETRIEVAL_RULE}
    except (ValueError,TypeError,AttributeError):
        raise HubError('X_OFFICIAL_MEDIA_RETRIEVAL_INVALID') from None


def validate_photo_retrieval(item,source_url):
    """No new fields means the immutable legacy exact-API-URL semantics."""
    present=[field in item for field in ('download_url','retrieval_rule')]
    if not any(present):return source_url
    expected=official_photo_retrieval(source_url)
    if not all(present) or any(item.get(key)!=value for key,value in expected.items()):
        raise HubError('X_OFFICIAL_MEDIA_RETRIEVAL_INVALID')
    return expected['download_url']
