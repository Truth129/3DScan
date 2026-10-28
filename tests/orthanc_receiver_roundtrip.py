"""Synthetic C-ECHO/C-STORE qualification in a private network namespace.

Run with a Python environment containing pynetdicom 3.0.4 and the app's imaging
packages. Enable only the new namespace's loopback interface, for example:

  receiver_host_ns=$(readlink /proc/self/ns/net)
  SCAN3D_PARENT_NET_NS="$receiver_host_ns" unshare --user --map-root-user --net \
    bash -c 'ip link set lo up; exec "$@"' -- \
    PYTHON tests/orthanc_receiver_roundtrip.py --output /tmp/receiver-v3.json

Set PYTHONPATH to the repository before invocation. No host archive is read;
only the pinned executable is reused. Output must be a new file beneath /tmp.
"""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
from pynetdicom import AE
from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind, Verification

from scripts import orthanc_receiver
from scripts import orthanc_seed_demo as seed

CALLING_AET = "OsiriX-mac"


def require_isolated_network():
    parent = os.environ.get("SCAN3D_PARENT_NET_NS")
    if not parent or os.readlink("/proc/self/ns/net") == parent or {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Run inside a new user/network namespace with only loopback enabled; no host listener is permitted")


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require_isolated_network()
    output = args.output.absolute()
    if not output.resolve().is_relative_to(Path("/tmp")) or output.exists() or output.is_symlink():
        parser.error("Output must be a new report file beneath /tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "synthetic_only": True,
        "network_namespace_isolated": True,
        "calling_aet": CALLING_AET,
        "fixture_generation": seed.FIXTURE_GENERATION,
        "sources_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in [Path(__file__), Path(seed.__file__)]},
        "checks": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="scan3d-orthanc-receiver-") as folder:
            directory = Path(folder)
            orthanc_receiver.prepare(directory, "127.0.0.1", CALLING_AET)
            config = json.loads((directory / "orthanc.json").read_text())
            password = config["RegisteredUsers"]["scan3d"]
            token = base64.b64encode(("scan3d:" + password).encode()).decode()

            def get(path, raw=False):
                with urlopen(Request("http://127.0.0.1:8044" + path, headers={"Authorization": "Basic " + token}), timeout=5) as response:
                    data = response.read()
                return data if raw else json.loads(data)

            log_path = output.with_suffix(".orthanc.log")
            with log_path.open("x") as log:
                log_path.chmod(0o600)
                process = subprocess.Popen([str(directory / "bin/Orthanc-1.13.0"), str(directory / "orthanc.json")], stdout=log, stderr=subprocess.STDOUT)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("Owned synthetic receiver exited")
                        try:
                            if get("/system")["Name"] == "3DScan incoming archive":
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("Synthetic receiver unavailable")

                    def associate(calling=CALLING_AET, called="SCAN3D", source=None):
                        ae = AE(ae_title=calling)
                        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = 3
                        ae.add_requested_context(Verification)
                        ae.add_requested_context(CTImageStorage, [ExplicitVRLittleEndian])
                        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
                        return ae.associate("127.0.0.1", 4243, ae_title=called, bind_address=(source or "", 0))

                    association = associate()
                    assert association.is_established
                    assert association.send_c_echo().Status == 0
                    originals = [pydicom.dcmread(io.BytesIO(seed.dicom_bytes(321, index))) for index in range(12)]
                    for image in originals:
                        assert association.send_c_store(image).Status == 0
                    assert association.send_c_store(originals[0]).Status == 0
                    association.release()
                    report["checks"]["allowed_echo_and_twelve_ct_store"] = True
                    instances = get("/instances")
                    assert len(instances) == 12
                    archived = {
                        str(pydicom.dcmread(io.BytesIO(get("/instances/" + identity + "/file", True))).SOPInstanceUID): identity for identity in instances
                    }
                    for image in originals:
                        saved = pydicom.dcmread(io.BytesIO(get("/instances/" + archived[str(image.SOPInstanceUID)] + "/file", True)))
                        assert saved.PixelData == image.PixelData
                        for field in (
                            "SOPClassUID",
                            "SOPInstanceUID",
                            "StudyInstanceUID",
                            "SeriesInstanceUID",
                            "FrameOfReferenceUID",
                            "ImageOrientationPatient",
                            "ImagePositionPatient",
                            "PixelSpacing",
                            "RescaleIntercept",
                            "RescaleSlope",
                        ):
                            assert getattr(saved, field) == getattr(image, field), field
                    report["checks"].update(pixels_source_uids_and_geometry_preserved=True, duplicate_does_not_add_instances=True)
                    for label, calling, called, source in (
                        ("unknown_calling_aet", "UNKNOWN", "SCAN3D", None),
                        ("wrong_case_calling_aet", CALLING_AET.upper(), "SCAN3D", None),
                        ("wrong_called_aet", CALLING_AET, "WRONG", None),
                        ("wrong_sender_ip", CALLING_AET, "SCAN3D", "127.0.0.2"),
                    ):
                        peer = associate(calling, called, source)
                        if peer.is_established:
                            status = peer.send_c_echo()
                            assert not status or getattr(status, "Status", None) != 0, label
                            peer.release()
                        report["checks"][label + "_rejected"] = True
                    peer = associate()
                    assert peer.is_established
                    query = Dataset()
                    query.QueryRetrieveLevel = "STUDY"
                    query.StudyInstanceUID = ""
                    responses = list(peer.send_c_find(query, StudyRootQueryRetrieveInformationModelFind))
                    assert not any(identifier is not None for _, identifier in responses)
                    assert not any(status and status.Status in {0, 0xFF00, 0xFF01} for status, _ in responses)
                    peer.release()
                    report["checks"]["query_retrieve_disallowed"] = True
                    series = get("/series")
                    assert len(series) == 1
                    item = get("/series/" + series[0])
                    assert item["Status"] == "Complete" and item["ExpectedNumberOfInstances"] == 12
                    report.update(passed=True, instances=12, series_status=item["Status"])
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    except BaseException as error:
        report.update(passed=False, error=str(error))
        raise
    finally:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "report": str(output)}))


if __name__ == "__main__":
    run()
