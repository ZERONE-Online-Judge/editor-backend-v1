"""Standard-library adapters for the frozen Editor execution engine."""
import hashlib
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
_settings = None


def configure(image):
    global _settings
    _settings = SimpleNamespace(execution_transport='local', execution_concurrency=1,
                                runner_image=image, render_image=image,
                                work_dir=Path(tempfile.mkdtemp(prefix='zoj-editor-replay-')))


def get_settings():
    if _settings is None: raise RuntimeError('Configure the replay image first')
    return _settings


def uid(): return uuid4().hex


def safe_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}',value) or '..' in value:
        raise ValueError('Unsafe package filename')
    return value


def checked(relative):
    path=ROOT/relative
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Package file leaves its root')
    return path.read_bytes()


def read_blob(sha):
    if not re.fullmatch(r'[a-f0-9]{64}',sha): raise ValueError('Invalid content hash')
    path=ROOT/'blobs'/sha
    if path.exists():
        content=checked('blobs/'+sha)
        if hashlib.sha256(content).hexdigest()!=sha: raise ValueError('Package blob checksum failed')
        return content
    for path in (ROOT/'replay/vendor/testlib').iterdir():
        if path.is_file():
            content=checked(str(path.relative_to(ROOT)))
            if hashlib.sha256(content).hexdigest()==sha: return content
    raise ValueError('Package blob is missing: '+sha)


def expanded_tests(testset):
    if testset.get('script'): raise ValueError('Linux replay requires materialized tests')
    return sorted(testset['tests'].values(),key=lambda test:test['index'])
