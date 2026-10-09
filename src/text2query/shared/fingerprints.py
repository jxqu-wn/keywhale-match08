"""Hash all implementation files, including each strategy and shared modules."""
import hashlib
from pathlib import Path


def implementation_hash():
    root=Path(__file__).resolve().parents[1]
    digest=hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()
