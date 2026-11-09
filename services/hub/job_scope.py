"""Parameter-bound handler scopes shared by Store and historical queue views.

None retains the legacy direct-call contract. Empty means no permissions, never
all types. Workers always supply an explicit tuple; queue-specific SQL guards
remain in force as an intersection with this whitelist.
"""
import re


def job_type_filter(job_types):
    if job_types is None:
        return '1=1', ()
    if not isinstance(job_types, (tuple, list, frozenset, set)):
        raise ValueError('JOB_TYPES_INVALID')
    if len(job_types) > 256 or any(not isinstance(t, str) or
            not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}', t) for t in job_types):
        raise ValueError('JOB_TYPES_INVALID')
    types = tuple(sorted(set(job_types)))
    if not types:
        return '0=1', ()
    return 'job_type IN (' + ','.join('?' for _ in types) + ')', types
