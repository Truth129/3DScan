from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .archive.settings import DicomSendSettings, DicomWebSettings

ROOT = Path(__file__).resolve().parent.parent
# Local, git-ignored settings; scan3d.env.example documents every variable.
CONFIG_FILE = ROOT / "scan3d.env"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9010
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})
# auto prefers a discrete GPU and uses the CPU when PyTorch sees none; see segmentation._device_info.
# It chooses within one PyTorch build, which sees only its vendor's GPUs. See inference_paths.
DEFAULT_DEVICE = "auto"
# Modalities with an installed image adapter (modalities._ADAPTERS).
MODALITIES = ("CT", "MR")
# What an installation offers unless SCAN3D_MODALITIES says otherwise. MRI is
# implemented but experimental, so it is opt-in.
DEFAULT_MODALITIES = ("CT",)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without evaluating them, so a value is never run as shell.

    The format is the subset systemd's EnvironmentFile= accepts as well: a value
    is the rest of its line (no inline comments), with optional matching quotes.
    Errors name the line only, because a value may be a password.
    """
    values = {}
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError(f"{path}:{number} is not a KEY=VALUE line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_environment(environ=None) -> Path | None:
    """Fill unset variables from the local config file, and return the file used.

    Real environment variables always win, so a service manager or a one-off shell
    override needs no edit here. Settings arrive through the environment because
    worker processes inherit it. SCAN3D_CONFIG names another file, and it must exist.
    """
    environ = os.environ if environ is None else environ
    configured = environ.get("SCAN3D_CONFIG", "").strip()
    path = Path(configured).expanduser() if configured else CONFIG_FILE
    if configured and not path.is_file():
        raise ValueError(f"SCAN3D_CONFIG does not exist: {path}")
    loaded = path if path.is_file() else None
    if loaded:
        for key, value in read_env_file(path).items():
            environ.setdefault(key, value)
    # TotalSegmentator keeps its weights, license and telemetry choice here, and
    # scripts/configure_totalseg.py must see the same directory as the workers.
    environ.setdefault("TOTALSEG_HOME_DIR", str(ROOT / ".runtime" / "totalsegmentator"))
    tools = ROOT / ".tools" / "bin"
    if tools.is_dir():
        environ["PATH"] = os.pathsep.join(filter(None, (str(tools), environ.get("PATH"))))
    return loaded


def resolve_auth(value, hosts) -> str:
    """The sign-in mode. Only loopback may run without sign-in, whatever anyone configures.

    A wildcard or 0.0.0.0 is not loopback, and neither is an empty listen address, which binds
    every interface. A proxy that rewrites Host hides a published address from this, so it must
    set password itself.
    """
    published = any(host not in LOOPBACK_HOSTS for host in hosts)
    mode = (value or "").strip().lower()
    if not mode:
        return "password" if published else "off"
    if mode not in ("off", "password"):
        raise ValueError("SCAN3D_AUTH must be off or password")
    if mode == "off" and published:
        raise ValueError("SCAN3D_AUTH=off is refused unless SCAN3D_HOST and SCAN3D_ALLOWED_HOSTS are all loopback")
    return mode


def device_request() -> str:
    return os.environ.get("SCAN3D_DEVICE", "").strip() or DEFAULT_DEVICE


def _dicomweb_settings():
    """Imported here, so GPU child processes that read settings never load the archive package."""
    from .archive.settings import DicomWebSettings

    return DicomWebSettings.from_env(orthanc_url=os.environ.get("SCAN3D_ORTHANC_URL", "").strip())


def _dicom_send_settings():
    from .archive.settings import DicomSendSettings

    return DicomSendSettings.from_env()


# One captured view. Multipart parsing buffers a body in RAM-backed /tmp before
# any handler runs, so this also bounds what one request can make the server hold.
VIEW_UPLOAD_LIMIT = 128 * 1024**2


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    import_roots: tuple[Path, ...]
    web_dir: Path = ROOT / "web" / "dist"
    upload_limit: int = 16 * 1024**3
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    modalities: tuple[str, ...] = MODALITIES
    orthanc_url: str = field(default="", repr=False)
    orthanc_username: str = field(default="", repr=False)
    orthanc_password: str = field(default="", repr=False)
    orthanc_export_destinations: tuple[str, ...] = ()
    auth: str = "off"
    orthanc_ca_bundle: str = ""
    dicomweb: "DicomWebSettings | None" = None
    dicom_send: "DicomSendSettings | None" = None

    @classmethod
    def from_env(cls):
        modalities = tuple(
            dict.fromkeys(part.strip().upper() for part in os.environ.get("SCAN3D_MODALITIES", ",".join(DEFAULT_MODALITIES)).split(",") if part.strip())
        )
        if not modalities or set(modalities) - set(MODALITIES):
            raise ValueError(f"SCAN3D_MODALITIES must list one or more of: {', '.join(MODALITIES)}")
        destinations = tuple(dict.fromkeys(part.strip() for part in os.environ.get("SCAN3D_ORTHANC_EXPORT_DESTINATIONS", "").split(",") if part.strip()))
        if any(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", part) is None for part in destinations):
            raise ValueError("SCAN3D_ORTHANC_EXPORT_DESTINATIONS must list configured Orthanc modality identifiers")
        ca_bundle = os.environ.get("SCAN3D_ORTHANC_CA_BUNDLE", "").strip()
        if ca_bundle and not Path(ca_bundle).is_absolute():
            raise ValueError("SCAN3D_ORTHANC_CA_BUNDLE must be an absolute path to a PEM file")
        allowed_hosts = tuple(p.strip() for p in os.environ.get("SCAN3D_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if p.strip())
        host = os.environ.get("SCAN3D_HOST", DEFAULT_HOST).strip()
        return cls(
            Path(os.environ.get("SCAN3D_DATA_DIR", ROOT / "data")).resolve(),
            tuple(Path(p).expanduser().resolve() for p in os.environ.get("SCAN3D_IMPORT_ROOTS", str(ROOT)).split(os.pathsep) if p),
            web_dir=Path(os.environ.get("SCAN3D_WEB_DIR", ROOT / "web" / "dist")).expanduser().resolve(),
            allowed_hosts=allowed_hosts,
            modalities=modalities,
            orthanc_url=os.environ.get("SCAN3D_ORTHANC_URL", "").strip(),
            orthanc_username=os.environ.get("SCAN3D_ORTHANC_USERNAME", ""),
            orthanc_password=os.environ.get("SCAN3D_ORTHANC_PASSWORD", ""),
            orthanc_export_destinations=destinations,
            auth=resolve_auth(os.environ.get("SCAN3D_AUTH"), (*allowed_hosts, host)),
            orthanc_ca_bundle=ca_bundle,
            dicomweb=_dicomweb_settings(),
            dicom_send=_dicom_send_settings(),
        )

    @property
    def sign_in_required(self) -> bool:
        # Anything but an explicit off asks for a session, so a mistyped value fails closed.
        return self.auth != "off"

    @property
    def view_upload_limit(self) -> int:
        return min(VIEW_UPLOAD_LIMIT, self.upload_limit)

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
