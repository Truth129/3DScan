"""Render the code-reviewed system overview as a self-contained SVG.

Editable detailed flowcharts live in ../ARCHITECTURE.md. No patient/case data
or runtime secrets are embedded in this diagram.
"""

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).with_name("architecture.svg")
parts = [
    """<svg xmlns="http://www.w3.org/2000/svg" width="1460" height="1480" viewBox="0 0 1460 1480" role="img" aria-labelledby="title description">
<title id="title">3DScan system architecture</title>
<desc id="description">Remote browser uses Cornerstone3D and Three.js with client RAM and GPU caches. Private HTTPS connects it to FastAPI on the workstation. One locked SQLite-backed worker set runs a GPU lane for segmentation and nnInteractive corrections, one job at a time, and a CPU lane that prepares images, applies direct edits, saves corrections and builds native-mask measurements and surfaces. ROCm inference uses the workstation GPU from child processes. SQLite, native files and derived caches persist separately. Exports include offline HTML, Slicer round trips and browser PNG. Live deployment is CT-only; static MRI is implemented on the expansion branch.</desc>
<defs><marker id="arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1L11 6L1 11Z" fill="#9bb7db"/></marker></defs>
<style>text{font-family:Inter,"DejaVu Sans",Arial,sans-serif}.title{fill:#edf2fc;font-weight:650;font-size:34px}.subtitle{fill:#a5b2c9;font-size:17px}.lane{font-size:14px;font-weight:650;letter-spacing:1.5px;fill:#93a8c9}.card-title{fill:#f0f4fc;font-size:20px;font-weight:650}.body{fill:#b8c4d8;font-size:16px}.edge-label{fill:#a7bddb;font-size:13px}.small{fill:#b8c4d8;font-size:14px}.edge{fill:none;stroke:#9bb7db;stroke-width:2;stroke-linejoin:round;marker-end:url(#arrow)}.both{marker-start:url(#arrow)}</style>
<rect width="1460" height="1480" fill="#0c111a"/>
"""
]


def text(x, y, value, cls="body", anchor=None):
    attr = f' text-anchor="{anchor}"' if anchor else ""
    parts.append(f'<text x="{x}" y="{y}" class="{cls}"{attr}>{escape(value)}</text>')


def region(x, y, w, h, label):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="15" fill="#101824" stroke="#29364b"/>')
    text(x + 24, y + 30, label, "lane")


def card(x, y, w, h, title, lines, accent="#7194c4"):
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#172131" stroke="#3b4a62"/>')
    parts.append(f'<rect x="{x}" y="{y + 12}" width="4" height="{h - 24}" rx="2" fill="{accent}"/>')
    text(x + 19, y + 31, title, "card-title")
    for i, line in enumerate(lines):
        text(x + 19, y + 58 + i * 22, line)


def edge(path, label=None, x=0, y=0, both=False):
    parts.append(f'<path d="{path}" class="edge{" both" if both else ""}"/>')
    if label:
        text(x, y, label, "edge-label", "middle")


text(40, 63, "3DScan · system architecture", "title")
text(40, 99, "Remote interaction stays local; the workstation owns processing and durable case data.", "subtitle")
region(40, 140, 1380, 260, "REMOTE DESKTOP BROWSER  ·  CLIENT RAM + CLIENT GPU")
region(40, 460, 1380, 430, "WORKSTATION  ·  FASTAPI + GPU AND CPU WORKER LANES + ROCm INFERENCE")
region(40, 980, 1380, 170, "DURABLE STATE  ·  SQLITE + FILESYSTEM")
region(40, 1210, 1380, 145, "EXCHANGE & EXPORTS")

# Browser-to-workstation request/data boundary, then queued processing lanes.
edge("M265 356V515", "LAN / WAN · private Tailscale HTTPS", 478, 435, True)
edge("M460 273H535", "linked", 498, 259)
edge("M925 273H1000", "source", 963, 259)
edge("M460 575H535", "jobs", 498, 560)
edge("M925 575H1000", "GPU tasks", 963, 560)
edge("M620 635V670H265V700", "open scan", 425, 659)
edge("M730 635V700", "CPU jobs", 770, 674)
edge("M1195 635V670H835V700", "native masks", 1060, 659)
edge("M460 772H535", "grid", 498, 756)
edge("M925 772H1000", "artifacts", 963, 756)
# Every route for source/derived data remains mediated by the API.
edge("M1390 730H1436V438H430V515", "authorized slabs, masks and mesh assets", 1120, 429)
edge("M265 845V914H615V980", "source image", 410, 904)
edge("M730 845V980", "native masks + new revision", 846, 925)
edge("M1195 845V980", "read / reuse / cache", 1275, 925, True)
edge("M70 575H24V1060H40", both=True)
edge("M730 1150V1210", "saved case artifacts", 830, 1185)

card(
    70,
    190,
    390,
    166,
    "Workspace controls",
    ["Vue 3 + TypeScript", "Case, series, task and panel controls", "Linked structure selection + crosshairs", "Review gestures and progress feedback"],
)
card(
    535,
    190,
    390,
    166,
    "CT review + client source cache",
    [
        "Cornerstone3D + reslice worker",
        "Preview → full native CT in client RAM",
        "Local scrolling, windowing and overlays",
        "DICOM parsing happens on the server",
    ],
)
card(
    1000,
    190,
    390,
    166,
    "3D on the client GPU",
    ["Three.js / WebGL2", "Segmentation surfaces + CT planes", "Ray-marched CT volume + display mask", "Warm navigation avoids image requests"],
)
card(
    70, 515, 390, 120, "FastAPI service", ["Imports, jobs, revisions and review", "Image / mask chunks, GLB and exports", "Local service behind private access"]
)
card(
    535,
    515,
    390,
    120,
    "Two worker lanes, one lock",
    ["GPU lane: segment + nnInteractive", "CPU lane: open, edits, saves, refine", "Progress, cancellation and recovery"],
)
card(
    1000,
    515,
    390,
    120,
    "ROCm inference · R9700",
    ["TotalSegmentator + nnU-Net", "nnInteractive in a supervised process", "PyTorch runs only in child processes"],
    "#c7a16d",
)
card(
    70,
    700,
    390,
    145,
    "1. Validate and prepare source",
    [
        "pydicom: index, select and check series",
        "dcm2niix: reference NIfTI + geometry",
        "One physical grid per source image",
        "Opening a scan does not segment it",
    ],
)
card(
    535,
    700,
    390,
    145,
    "2. Native processing · CPU",
    [
        "Geometry, QC and voxel measurements",
        "Direct edits, cut and refinement rules",
        "VTK surfaces → GLB for saved masks",
        "Accept corrections into a new revision",
    ],
    "#a69bd0",
)
card(
    1000,
    700,
    390,
    145,
    "3. Prepare browser artifacts",
    ["Compressed scalar slabs + mask crops", "3D display union capped at 8 MiB", "Content-addressed caches and GLB", "Presentation never rewrites masks"],
)

# Storage is one logical authority, with distinct records and native files.
text(70, 1042, "SQLite records", "card-title")
text(70, 1070, "Cases · image assets · jobs", "body")
text(70, 1094, "Revisions · review sessions", "body")
text(535, 1042, "Native case files", "card-title")
text(535, 1070, "Copied DICOM · reference NIfTI", "body")
text(535, 1094, "Voxel masks · provenance · logs", "body")
text(1000, 1042, "Derived caches", "card-title")
text(1000, 1070, "Conversion · inference · meshes", "body")
text(1000, 1094, "Scalar slabs · display unions", "body")
text(70, 1128, "Authority: native image + voxel masks. Meshes and display unions do not change measurements.", "small")

text(70, 1274, "Offline HTML", "card-title")
text(70, 1302, "Chosen segmentation / volume / CT plane", "body")
text(70, 1326, "Self-contained viewer; no server needed", "body")
text(535, 1274, "Slicer correction round trip", "card-title")
text(535, 1302, "Source CT + named .seg.nrrd masks", "body")
text(535, 1326, "Geometry checked before a new revision", "body")
text(1000, 1274, "PNG capture", "card-title")
text(1000, 1302, "Generated by the browser renderer", "body")
text(1000, 1326, "Current view and presentation settings", "body")

parts.append('<rect x="40" y="1390" width="1380" height="70" rx="10" fill="#121b28" stroke="#627390" stroke-dasharray="7 5"/>')
text(60, 1418, "Deployment boundary · 27 October 2026", "small")
text(60, 1442, "Live: CT-only. Static 3D MRI is implemented and tested on expansion; other imaging adapters remain future work.", "small")
parts.append("</svg>")
OUT.write_text("\n".join(parts) + "\n")
print(OUT)
