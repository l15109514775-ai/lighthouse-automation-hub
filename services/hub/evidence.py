from collections import Counter
import hashlib
import re
import unicodedata
from .db import encode


def sha256_file(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def machine_evidence(source, translation, qa, glossary=None):
    original = source['post']['text']
    normalized_source = unicodedata.normalize('NFKC', original)
    normalized_translation = unicodedata.normalize('NFKC', translation)
    def matches(pattern, text):
        return Counter(re.findall(pattern, text))
    checks = {
        'nonempty': bool(translation.strip()),
        'numbers': matches(r'\d+(?:[.:/]\d+)*', normalized_source) == matches(r'\d+(?:[.:/]\d+)*', normalized_translation),
        'linebreaks': original.count('\n') == translation.count('\n'),
        'urls': matches(r'https?://\S+', original) == matches(r'https?://\S+', translation),
        'mentions': matches(r'@[A-Za-z0-9_]+', original) == matches(r'@[A-Za-z0-9_]+', translation),
        'glossary': all(target in translation for name, target in (glossary or {}).items() if name in original),
        'timestamp_present': bool(source['post'].get('created_at')),
        'reference_ids_preserved': [x['id'] for x in source['post'].get('referenced_tweets', [])]
    }
    failed = [name for name, value in checks.items() if value is False]
    return {'version':'machine-evidence-v1', 'source_sha256':hashlib.sha256(encode(source).encode()).hexdigest(),
            'translation_sha256':hashlib.sha256(translation.encode()).hexdigest(), 'checks':checks,
            'deterministic_failures':failed, 'independent_model_advice':qa,
            'machine_checks_passed':not failed and qa.get('passed') is True and not qa.get('issues'),
            'human_review':False, 'auto_publish_eligible':False,
            'limitations':['Machine advice cannot confirm source screenshots or human review.']}
