# Contributing

Thank you for helping. This integration handles health data, so a few rules are strict. Please read [the rules](#the-rules) before you open a pull request.

## Development setup

You need Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv venv --python 3.14 .venv
uv pip install -r requirements_test.txt
```

`requirements_test.txt` installs `pytest-homeassistant-custom-component`, which pulls in the matching Home Assistant and pytest, and `ruff`. This is the same setup the CI uses.

Run the tests and the linter from the repository root:

```sh
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

`.venv/bin/ruff format .` fixes formatting. The tests do not need network access or a Tidepool account.

## Project layout

```text
custom_components/tidepool/
  __init__.py       Entry setup and unload, platforms, child device removal
  api.py            Async client for the Tidepool REST API and its error types
  auth.py           Email and password sign-in, session token handling
  binary_sensor.py  Delivery and data source connectivity sensors
  config_flow.py    Sign-in, account choice, reauth, reconfigure and options flows
  const.py          Domain, URLs, intervals, service names and the sign-in notice
  coordinator.py    Fast and slow coordinators, repair issue, error mapping
  devices.py        Patient device and the pump, CGM and data source devices
  diagnostics.py    Counts-only, redacted diagnostics
  entity.py         Base entity that follows its device
  event.py          Alarm, meal and bolus event entities
  models.py         Parsing of Tidepool records and summaries, pure functions
  sensor.py         Glucose, summary, pump, settings and timestamp sensors
  services.py       tidepool.refresh and tidepool.import_history
  statistics.py     Hourly long-term glucose statistics
  services.yaml     Service definitions
  strings.json      Texts; translations/en.json must be identical
  manifest.json     Integration manifest (the version lives here)
  brand/icon.png    Integration icon
tests/              Tests, with synthetic fixtures in tests/fixtures/
scripts/            live_check.py and install_dev.sh (below)
.github/            CI, release workflow and issue templates
```

## The rules

- **Synthetic fixtures only.** Test data is invented. Never commit real data, tokens, passwords, emails, names, user ids, device ids, serial numbers or screenshots of a real account, not even "anonymised" ones. Not in code, tests, fixtures, docs, commit messages or issues.
- **No dose computation.** The integration shows what Tidepool recorded. It does not estimate, predict, suggest or calculate insulin, carbs or glucose targets. Values that come from a past decision stay labelled "as of the last dosing decision".
- **Wording.** Entity names, attributes and texts must not use "recommended" or "correction". Tests check this.
- **Read-only.** The integration only reads from Tidepool, and only from `api.tidepool.org`. No telemetry, no other hosts.
- **Texts.** `strings.json` and `translations/en.json` must be identical. Copy one over the other after you edit.
- **No secrets or ids in logs and errors.** Error messages carry an HTTP status and a path pattern, never a value.
- **Docs follow the code.** If you add or change an entity, update the entity tables in `README.md` (`tests/test_docs.py` checks the keys and the default on or off state against the code) and `CHANGELOG.md`.

## Checking against the real service

`scripts/live_check.py` runs the API layer against Tidepool with your own account. It prints structure only: record types, counts, key names and HTTP status codes. It never prints a value, an id or a time.

```sh
TIDEPOOL_EMAIL=you@example.com TIDEPOOL_PASSWORD=... .venv/bin/python scripts/live_check.py
```

Use a Tidepool account you are allowed to use for this, and do not paste its output anywhere without reading it first.

## Trying it in Home Assistant

`scripts/install_dev.sh` copies the integration to a Home Assistant host over ssh (it needs `tar` on the host, not rsync):

```sh
scripts/install_dev.sh --dry-run <ha-host>   # shows what it would do
scripts/install_dev.sh <ha-host>
```

It replaces `/config/custom_components/tidepool` on the host. It does **not** restart Home Assistant; do that yourself at a safe time.

## Releasing

1. Update `version` in `custom_components/tidepool/manifest.json` (and `pyproject.toml`) and add the version to `CHANGELOG.md`.
2. Merge to `main` with the CI green (tests, ruff, hassfest and HACS validation).
3. Push a tag `vX.Y.Z` that matches the manifest version. The release workflow checks the match, builds `tidepool.zip` and creates the GitHub release with generated notes. HACS installs from the tag, so `zip_release` is not used.

## Pull request checklist

- [ ] Tests added or updated, and `.venv/bin/pytest` passes.
- [ ] `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` are clean.
- [ ] Only synthetic data in fixtures, tests, docs and the description.
- [ ] No dose computation, and no "recommended" or "correction" in names or texts.
- [ ] `strings.json` and `translations/en.json` are identical.
- [ ] `README.md` (entities, services, privacy) and `CHANGELOG.md` updated where behaviour changed.
- [ ] Nothing new is sent anywhere other than `api.tidepool.org`.
