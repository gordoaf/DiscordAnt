"""Atomic exports with optional GUI-authorized ownership checks."""
import contextlib
import contextvars
import hashlib
import os
from pathlib import Path
import tempfile

_authorization = contextvars.ContextVar("export_authorization", default=None)


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def same_path(a, b):
    if path_key(a) == path_key(b):
        return True
    try:
        return Path(a).samefile(b)
    except OSError:
        return False


def fingerprint(path):
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def authorized(expected, on_written=None):
    """Allow only these paths and versions for one GUI job's exports."""
    token = _authorization.set((dict(expected), on_written))
    try:
        yield
    finally:
        _authorization.reset(token)


def check_paths(paths):
    auth = _authorization.get()
    if auth is None:
        return
    expected, _ = auth
    for path in paths:
        key = path_key(path)
        if key not in expected or fingerprint(path) != expected[key]:
            raise ValueError(f"Output changed or was not approved: {Path(path).name}. "
                             "Choose a different file or approve it again.")


def write_text(path, text, encoding="utf-8"):
    path = Path(path)
    check_paths([path])
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding,
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Collection can take hours: protect a destination changed meanwhile.
        check_paths([path])
        os.replace(tmp, path)
        auth = _authorization.get()
        if auth is not None:
            expected, callback = auth
            expected[path_key(path)] = fingerprint(path)
            if callback:
                callback(dict(expected))
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
