# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## 0.1.0

First release.

- Sign in with a Tidepool email and password (Tidepool's documented developer
  login), with reauthentication, reconfiguration and a repair issue if the
  sign-in is withdrawn.
- Choose which accounts to show: your own and accounts shared with you.
- Latest glucose reading and its age, and glucose summaries (time in range,
  average, GMI, variability and more) for 24 hours, 7, 14 and 30 days.
- Pump values: basal rate and mode, last bolus, 24 hour insulin and carbs,
  delivery state, insulin and carbs on board as of the last dosing decision.
- Pump and CGM settings, the bolus calculator's last entry, reported states and
  water intake as optional entities that are off by default.
- Event entities for alarms, meals and boluses.
- Devices for the pump, the CGM and the data sources behind the records.
- Once the pump and CGM are identified, their entities appear under those
  child devices, so friendly names look like "<model> Glucose" while entity
  ids stay based on the patient's name (for example
  `sensor.<patient>_glucose`).
- Services `tidepool.refresh` and `tidepool.import_history` (hourly glucose
  statistics of the past 1 to 90 days).
- Options for the refresh interval (2 to 30 minutes) and the accounts.
- Counts-only diagnostics.
