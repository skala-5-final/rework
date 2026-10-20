import hashlib
import re
from pathlib import Path

from rework.config import get_settings


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-가-힣]", "_", name)[:120]


def to_ref(path: Path) -> str:
    """DB에는 storage_dir 기준 상대경로를 저장한다. 로컬·컨테이너마다 storage_dir가 달라도 같은 파일을 가리킨다."""
    return Path(path).relative_to(get_settings().storage_dir).as_posix()


def resolve(ref: str) -> Path:
    """DB에 저장된 경로를 현재 실행 환경의 실제 경로로 바꾼다."""
    p = Path(ref)
    if not p.is_absolute():
        return get_settings().storage_dir / p
    # 상대경로로 바꾸기 전에 저장된 행: 절대경로의 var/storage 뒤쪽만 현재 storage_dir에 붙인다.
    parts = p.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i - 1:i + 1] == ("var", "storage"):
            return get_settings().storage_dir.joinpath(*parts[i + 1:])
    return p


def save_bytes(tenant_id: str, file_id: str, name: str, data: bytes) -> tuple[str, str]:
    """파일을 저장하고 (DB에 넣을 상대경로, sha256)을 돌려준다."""
    folder = get_settings().storage_dir / tenant_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}_{_safe(name)}"
    path.write_bytes(data)
    return to_ref(path), hashlib.sha256(data).hexdigest()


def artifact_dir(tenant_id: str, run_id: str) -> Path:
    folder = get_settings().storage_dir / tenant_id / "artifacts" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
