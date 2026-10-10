"""Atomic UTF-8 file storage and the shared local workspace lock."""
import json
import hashlib
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_lock(root):
    """Share the same OS lock between CLI collection and dashboard config writes."""
    import msvcrt
    (root/'data').mkdir(parents=True, exist_ok=True)
    with (root/'data'/'run.lock').open('a+b') as lock:
        if (root/'data'/'run.lock').stat().st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('已有本地运行正在进行，请等待完成后再操作') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def replace_after_readers(temporary,path):
    for attempt in range(3):
        try:
            os.replace(temporary,path)
            return
        except PermissionError:
            if attempt==2:raise
            time.sleep(.05*(attempt+1))


def _write_atomic(path, value, binary):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        if binary:
            temporary.write_bytes(value)
        else:
            temporary.write_text(value, encoding="utf-8")
        replace_after_readers(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(path: Path, value: str) -> None:
    _write_atomic(path,value,False)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def write_bytes(path, value):
    _write_atomic(path,value,True)


def json_digest(value):
    """Stable JSON SHA-256; preserve spacing/Unicode for existing cache identities."""
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_optional_json(path, errors):
    """Optional attachments can fail without hiding a valid core publication."""
    try:
        value = read_json(path,{})
        if not isinstance(value,dict):
            raise ValueError('需要JSON对象')
        return value
    except (ValueError,UnicodeError,OSError):
        errors.append(f'{path.name} 损坏或无法读取，请恢复备份；核心正式报告保留')
        return {}

