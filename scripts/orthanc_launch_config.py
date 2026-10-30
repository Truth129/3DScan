"""Print an Explorer 2 series-launch fragment; never read or change live config."""

import argparse
import json
from urllib.parse import urlsplit, urlunsplit

from scan3d.orthanc import OrthancClient


def configuration(orthanc_url: str, scan3d_url: str, *, launcher_path: str | None = None) -> dict:
    """Bind the button to the backend's configured archive, without credentials."""
    try:
        parsed = urlsplit(scan3d_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("Supply an absolute HTTP(S) 3DScan URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(char.isspace() or ord(char) < 32 for char in scan3d_url)
    ):
        raise ValueError("Supply the 3DScan root URL without credentials, query or fragment")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if port is not None:
        host += f":{port}"
    root = urlunsplit((parsed.scheme, host, "/", "", ""))
    if launcher_path is not None:
        if launcher_path != "/3dscan-launch/index.html":
            raise ValueError("Use the configured /3dscan-launch/index.html launcher path")
        # Explorer 2 lives under /ui/app/. Keep its browser origin and port
        # until the same-origin launcher chooses local or private HTTPS access.
        root = "../.." + launcher_path
    with OrthancClient(orthanc_url) as client:
        source_key = client.source_key
    return {
        "OrthancExplorer2": {
            "UiOptions": {
                "CustomButtons": {
                    "series": [
                        {
                            "HttpMethod": "GET",
                            "Id": "open-in-3dscan",
                            "Tooltip": "Open in 3DScan",
                            "Target": "_blank",
                            "Icon": "bi bi-box",
                            "Url": root + "#orthanc-series={UUID}&orthanc-source=" + source_key,
                        }
                    ]
                }
            }
        }
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orthanc-url", required=True, help="Exact server-side SCAN3D_ORTHANC_URL (no credentials)")
    parser.add_argument("--scan3d-url", required=True, help="Browser-accessible 3DScan root URL")
    parser.add_argument("--launcher-path", help="Use /3dscan-launch/index.html for automatic local/private access")
    args = parser.parse_args()
    try:
        result = configuration(args.orthanc_url, args.scan3d_url, launcher_path=args.launcher_path)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
