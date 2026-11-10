import re


class LeaseLost(RuntimeError):
    def __init__(self):
        super().__init__('LEASE_LOST')


def stable_error_code(code):
    if code is None:
        return None
    return code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code) else 'ERROR_CODE_INVALID'


def error_category(code):
    """Operational taxonomy; existing codes/states remain the durable contract."""
    code = stable_error_code(code) or ''
    categories = (
        ('database', ('SQLITE', 'DATABASE')),
        ('disk_resource', ('DISK_', 'WAITING_RESOURCE', 'HEAVY_RESOURCE_BUSY')),
        ('authorization', ('AUTH_EXPIRED', 'SESSION_', 'AUTHORIZATION')),
        ('platform_restriction', ('ACCESS_RESTRICTED', 'RATE_LIMIT', 'CAPTCHA', 'REGION', 'DRM', 'BOT_', 'FORBIDDEN')),
        ('identity', ('BINDING_', 'IDENTITY_', 'SOURCE_CHANGED', 'INPUT_CHANGED', 'LEASE_', 'INVENTORY_CHANGED')),
        ('integrity', ('RECEIPT_', 'PROBE_', 'FFPROBE', 'PACKET_', 'HASH_', 'CORRUPT', 'TRUNCAT', 'QUALITY_')),
        ('configuration', ('CONFIG', 'POLICY_', 'RUNTIME_UNAVAILABLE', 'WORKSPACE_', 'PERMISSION', 'NOT_WRITABLE')),
        ('network', ('NETWORK_', 'TIMEOUT', 'HELPER_INTERRUPTED')),
    )
    return next((category for category, fragments in categories if any(f in code for f in fragments)), 'internal')


class HubError(Exception):
    def __init__(self, code, *, retryable=False, review=False, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.review = review
        self.retry_after = retry_after

    @property
    def category(self):
        return error_category(self.code)


class ResourceDeferred(HubError):
    """A typed, local resource wait; only explicitly registered handlers use it."""
    def __init__(self,*,resource,required_bytes,binding_sha256):
        import re
        if (resource!='disk' or type(required_bytes) is not int or not 0<=required_bytes<=64_000_000_000 or
                not isinstance(binding_sha256,str) or not re.fullmatch('[a-f0-9]{64}',binding_sha256)):
            raise ValueError('INVALID_RESOURCE_DEFERRAL')
        super().__init__('WAITING_RESOURCE')
        self.resource=resource
        self.required_bytes=required_bytes
        self.binding_sha256=binding_sha256


class YouTubeReadinessDeferred(HubError):
    def __init__(self,reason,observation):
        if reason not in ('YOUTUBE_NATURAL_POST_LIVE_NOT_READY','YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY') or not isinstance(observation,dict):
            raise ValueError('INVALID_YOUTUBE_READINESS_DEFERRAL')
        super().__init__('WAITING_YOUTUBE_READINESS')
        self.reason,self.observation=reason,observation


class PublishUnknown(HubError):
    def __init__(self):
        super().__init__('PUBLISH_UNKNOWN')


class PublishRejected(HubError):
    """A documented explicit rejection, distinct from a lost submission result."""
    pass
