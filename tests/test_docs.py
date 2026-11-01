"""Guards for the public docs: the README must stay true to the code.

Scheme for the README's entity tables: a row starts with the entity's
translation key in backticks (the key under ``entity.<platform>`` in
strings.json), and its last cell is the default. Summary rows list the periods
that are on by default (``24 h, 14 d`` or ``none``); every other row says
``On`` or ``Off``. The tests compare both with the entity descriptions.
"""

import json
import re
from pathlib import Path

import yaml

from custom_components.tidepool import binary_sensor, event, sensor
from custom_components.tidepool.const import PASSWORD_DISCLAIMER

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / "tidepool"
README = (ROOT / "README.md").read_text()

ROW = re.compile(r"^\| `([a-z0-9_]+)` \|(.*)\|\s*$")


def _doc_paths() -> list[Path]:
    paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "CHANGELOG.md"]
    paths += sorted((ROOT / ".github").rglob("*.yml"))
    return paths


def _anchors() -> set[str]:
    """GitHub's anchors for the README's headings."""
    headings = re.findall(r"^#+ (.+)$", README, re.M)
    return {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in headings}


def _entity_rows() -> dict[str, str]:
    """The README's entity table rows: key -> default cell."""
    section = README.split("\n## Entities\n", 1)[1].split("\n## Services\n", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if match := ROW.match(line):
            key, rest = match.groups()
            assert key not in rows, f"{key} is listed twice"
            rows[key] = rest.rsplit("|", 1)[-1].strip()
    return rows


def _strings_keys() -> set[str]:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    return {key for entities in strings["entity"].values() for key in entities}


def _described() -> tuple[dict[str, bool], dict[str, list[str]]]:
    """Enabled by default per key, and the summary periods that are on."""
    plain: dict[str, bool] = {}
    for description in (
        sensor.GLUCOSE,
        sensor.GLUCOSE_AGE,
        sensor.LAST_UPLOAD,
        *sensor.PUMP_SENSORS,
        *sensor.RECORD_SENSORS,
        *sensor.LAST_DATA_SENSORS,
        *sensor.DEVICE_EVENT_SENSORS,
        *binary_sensor.DELIVERY_SENSORS,
        *event.EVENTS,
        binary_sensor._source_description("source", {}),
    ):
        plain[description.translation_key] = description.entity_registry_enabled_default
    summaries: dict[str, list[str]] = {}
    for description in sensor.SUMMARY_SENSORS:
        periods = summaries.setdefault(description.translation_key, [])
        if description.entity_registry_enabled_default:
            periods.append(sensor.PERIOD_LABELS[description.period])
    return plain, summaries


def test_readme_has_the_sign_in_text_and_heading() -> None:
    assert PASSWORD_DISCLAIMER in README
    # The repair issue links to this anchor.
    assert re.search(r"^## Sign-in and limitations$", README, re.M)
    assert "#sign-in-and-limitations" in (COMPONENT / "const.py").read_text()


def test_readme_says_it_is_independent_and_read_only() -> None:
    text = README.lower()
    assert "not affiliated" in text
    assert "not a medical device" in text
    assert "read-only" in text


def test_readme_has_no_oauth_setup() -> None:
    text = README.replace(PASSWORD_DISCLAIMER, "").lower()
    for phrase in ("oauth setup", "client id", "application credentials"):
        assert phrase not in text


def test_docs_have_no_personal_identifiers() -> None:
    for path in _doc_paths():
        text = path.read_text().lower()
        for private in ("charlie", "boulware", "192.168.", "homeassistant-config"):
            assert private not in text, f"{private} in {path.name}"


def test_readme_links_within_the_page_resolve() -> None:
    links = set(re.findall(r"\]\(#([a-z0-9-]+)\)", README))
    assert links
    assert links <= _anchors(), sorted(links - _anchors())


def test_readme_names_every_service() -> None:
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert services
    for name in services:
        assert f"tidepool.{name}" in README


def test_readme_entity_keys_exist_and_all_are_documented() -> None:
    documented = set(_entity_rows())
    known = _strings_keys()
    assert documented <= known, sorted(documented - known)
    assert known <= documented, sorted(known - documented)


def test_readme_defaults_match_the_code() -> None:
    rows = _entity_rows()
    plain, summaries = _described()
    for key, cell in rows.items():
        if key in summaries:
            periods = summaries[key]
            assert cell == (", ".join(periods) or "none"), key
        else:
            assert cell in ("On", "Off"), key
            assert (cell == "On") is plain[key], key
    assert set(rows) == set(plain) | set(summaries)


def test_readme_documents_the_limits() -> None:
    assert "2 to 30 minutes" in README
    assert "from 1 to 90" in README


def test_changelog_lists_the_manifest_version() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## {re.escape(manifest['version'])}\b", changelog, re.M)


def test_issue_templates() -> None:
    config = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text())
    assert config["blank_issues_enabled"] is False
    for link in config["contact_links"]:
        assert link["url"].split("#", 1)[1] in _anchors()
    bug = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/bug.yml").read_text())
    kinds = [item["type"] for item in bug["body"]]
    assert "checkboxes" in kinds
    warning = bug["body"][0]["attributes"]["value"].lower()
    for word in ("health data", "glucose", "ids", "passwords", "tokens"):
        assert word in warning


def test_release_workflow_and_hacs_metadata() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    # PyYAML reads the key ``on`` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {"contents": "write"}
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert "zip_release" not in hacs
