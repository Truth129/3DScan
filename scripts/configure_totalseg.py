"""Disable anonymous usage telemetry without changing package-global settings."""

import json
import os
from pathlib import Path
import uuid

root = Path(__file__).resolve().parents[1]
directory = Path(os.environ.get("TOTALSEG_HOME_DIR", root / ".runtime/totalsegmentator"))
directory.mkdir(parents=True, exist_ok=True)
config_path = directory / "config.json"
if config_path.exists():
    config = json.loads(config_path.read_text())
else:
    # These are upstream's initial config keys. Keep this independent of torch
    # so telemetry stays disabled even while native ROCm libraries are pending.
    config = {"totalseg_id": "totalseg_" + uuid.uuid4().hex[:8].upper(), "prediction_counter": 0}
config["send_usage_stats"] = False
config_path.write_text(json.dumps(config, indent=2) + "\n")
print(f"TotalSegmentator usage statistics disabled: {config_path}")
