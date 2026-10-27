from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Modalities with an installed image adapter (modalities._ADAPTERS). A deployment
# can offer fewer through SCAN3D_MODALITIES; production stays CT-only until MRI
# is qualified there.
MODALITIES = ("CT", "MR")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    import_roots: tuple[Path, ...]
    web_dir: Path = ROOT / "web" / "dist"
    upload_limit: int = 16 * 1024**3
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    modalities: tuple[str, ...] = MODALITIES

    @classmethod
    def from_env(cls):
        modalities = tuple(dict.fromkeys(part.strip().upper() for part in os.environ.get("SCAN3D_MODALITIES", ",".join(MODALITIES)).split(",") if part.strip()))
        if not modalities or set(modalities) - set(MODALITIES):
            raise ValueError(f"SCAN3D_MODALITIES must list one or more of: {', '.join(MODALITIES)}")
        return cls(
            Path(os.environ.get("SCAN3D_DATA_DIR", ROOT / "data")).resolve(),
            tuple(Path(p).expanduser().resolve() for p in os.environ.get("SCAN3D_IMPORT_ROOTS", str(ROOT)).split(os.pathsep) if p),
            allowed_hosts=tuple(p.strip() for p in os.environ.get("SCAN3D_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if p.strip()),
            modalities=modalities,
        )

    def modality_unavailable(self, modality) -> str | None:
        """Why this workstation does not offer a modality's workflows, or None.

        A deployment policy applied on use: stored series keep their source facts,
        so enabling a modality later makes previously imported series available.
        """
        return None if str(modality) in self.modalities else f"{modality} images are not enabled on this workstation"

    def import_path(self, path: str) -> Path:
        candidate = Path(path).expanduser().resolve()
        if not any(candidate == root or root in candidate.parents for root in self.import_roots):
            raise ValueError("Path is outside configured SCAN3D_IMPORT_ROOTS")
        if not candidate.exists():
            raise ValueError("Import path does not exist")
        return candidate
