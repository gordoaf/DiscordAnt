"""Small JSON state files: validated reads, atomic writes, last-good backup."""
import json
import os
from pathlib import Path
import tempfile


def _replace(path, payload):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def load(path, validate, default, warn=lambda message: None):
    path = Path(path)
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if not validate(value):
                raise ValueError("unexpected data structure")
            if candidate != path:
                warn(f"Recovered {path.name} from its last-good backup.")
            return value
        except (ValueError, OSError, TypeError) as exc:
            warn(f"Could not read {candidate.name}: {exc}")
    return default


def save(path, value, validate=None):
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = path.read_bytes()
        try:
            decoded = json.loads(previous.decode("utf-8-sig"))
            if type(decoded) is not type(value) or (validate and not validate(decoded)):
                raise ValueError("unexpected previous data structure")
        except (ValueError, UnicodeError):
            previous = None
        if previous is not None:
            _replace(path.with_suffix(path.suffix + ".bak"), previous)
    _replace(path, payload)
