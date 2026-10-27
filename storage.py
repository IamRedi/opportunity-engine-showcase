"""Abstraksion i ruajtjes (Faza 8A) — provider lokal, i gatshëm për object storage.

CV logic nuk prek më path-e filesystem-i drejtpërdrejt: përdor save/open/
delete/exists/metadata me çelësa relativë. Path traversal refuzohet.
Provider-e të ardhshëm (S3 dhe kundër) implementojnë të njëjtën ndërfaqe.
"""
from pathlib import Path
import os
import mimetypes

import config


class LocalStorage:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # path traversal refuzohet: pa "..", pa absolute, duhet të jetë brenda base
        key = (key or "").strip().lstrip("/")
        if not key or ".." in key.split("/") or "\\" in key or ":" in key:
            raise ValueError(f"çelës i pavlefshëm ruajtjeje: {key!r}")
        p = (self.base / key).resolve()
        if self.base.resolve() not in p.parents and p != self.base.resolve():
            raise ValueError("path traversal refuzuar")
        return p

    def save(self, key: str, data: bytes) -> dict:
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return self.metadata(key)

    def open(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def delete(self, key: str) -> bool:
        p = self._resolve(key)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def metadata(self, key: str) -> dict:
        p = self._resolve(key)
        if not p.exists():
            return {"key": key, "exists": False, "path": str(p)}
        import hashlib
        data = p.read_bytes()
        return {"key": key, "exists": True, "size": len(data), "path": str(p),
                "sha256": hashlib.sha256(data).hexdigest()}


class PrivateBlobStorage:
    """Private cloud objects are only served through authenticated app endpoints."""
    def _key(self, key):
        if not key or key.startswith('/') or '..' in key.split('/') or '\\' in key or ':' in key:
            raise ValueError('Invalid storage key')
        return key

    def save(self, key, data):
        from vercel import blob
        key = self._key(key)
        blob.put(key, data, access='private', add_random_suffix=False, overwrite=True,
                 content_type=mimetypes.guess_type(key)[0] or 'application/octet-stream')
        return {'key':key,'exists':True,'size':len(data),'path':'blob:'+key}

    def open(self, key):
        from vercel import blob
        from vercel.blob.errors import BlobError
        try:
            return bytes(blob.get(self._key(key), access='private', use_cache=False))
        except BlobError:
            raise OSError('Private file is unavailable') from None

    def delete(self, key):
        from vercel import blob
        blob.delete(self._key(key))
        return True

    def exists(self, key):
        return self.metadata(key)['exists']

    def metadata(self, key):
        from vercel import blob
        from vercel.blob.errors import BlobNotFoundError
        key = self._key(key)
        try:
            head = blob.head(key)
            return {'key':key,'exists':True,'size':head.size,'path':'blob:'+key}
        except BlobNotFoundError:
            return {'key':key,'exists':False,'path':'blob:'+key}


def read_reference(reference):
    """Resolve server-stored CV references; never fetch arbitrary browser-provided URLs."""
    reference = str(reference)
    if reference.startswith('blob:'):
        return get_storage().open(reference[5:])
    if os.environ.get('STORAGE_PROVIDER') == 'vercel-blob':
        raise OSError('A cloud CV must have a private object reference')
    return Path(reference).read_bytes()


def get_storage():
    if os.environ.get('STORAGE_PROVIDER',config.STORAGE_PROVIDER) == 'vercel-blob':
        return PrivateBlobStorage()
    # Defaults to BASE_DIR; also isolates local CV test workspaces.
    return LocalStorage(Path(config.DATA_DIR) / "uploads")
