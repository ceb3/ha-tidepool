"""Static checks on the integration manifest and HACS metadata."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_manifest_fields() -> None:
    """The manifest declares the expected domain, type and requirements."""
    manifest = json.loads(
        (ROOT / "custom_components" / "tidepool" / "manifest.json").read_text()
    )
    assert manifest["domain"] == "tidepool"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["integration_type"] == "service"
    assert manifest["requirements"] == []
    assert "dependencies" not in manifest
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])

    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2025.10.0"
