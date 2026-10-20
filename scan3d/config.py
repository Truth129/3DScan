from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    import_roots: tuple[Path, ...]
    web_dir: Path = ROOT / "web" / "dist"
    upload_limit: int = 16 * 1024**3
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.environ.get("SCAN3D_DATA_DIR", ROOT / "data")).resolve(),
            tuple(Path(p).expanduser().resolve() for p in os.environ.get("SCAN3D_IMPORT_ROOTS", str(ROOT)).split(os.pathsep) if p),
            allowed_hosts=tuple(p.strip() for p in os.environ.get("SCAN3D_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if p.strip()),
        )

    def import_path(self, path: str) -> Path:
        candidate = Path(path).expanduser().resolve()
        if not any(candidate == root or root in candidate.parents for root in self.import_roots):
            raise ValueError("Path is outside configured SCAN3D_IMPORT_ROOTS")
        if not candidate.exists():
            raise ValueError("Import path does not exist")
        return candidate
