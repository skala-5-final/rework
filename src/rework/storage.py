import hashlib
import re
from pathlib import Path

from rework.config import get_settings


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-가-힣]", "_", name)[:120]


def save_bytes(tenant_id: str, file_id: str, name: str, data: bytes) -> tuple[Path, str]:
    folder = get_settings().storage_dir / tenant_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}_{_safe(name)}"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def artifact_dir(tenant_id: str, run_id: str) -> Path:
    folder = get_settings().storage_dir / tenant_id / "artifacts" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
