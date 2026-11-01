# Tidepool for Home Assistant

A custom [Home Assistant](https://www.home-assistant.io/) integration that reads data from a [Tidepool](https://www.tidepool.org/) account and shows it as entities: the latest glucose reading, summaries of time in range, pump and insulin records, and events for alarms, meals and boluses.

It is a community project. It is independent of Tidepool and not affiliated with, endorsed by, or supported by Tidepool. The integration's icon is Tidepool's public icon; it is there only to identify the Tidepool service in Home Assistant.

## What it is, and what it is not

- It is **read-only**. It only fetches data from Tidepool. It never uploads, changes or deletes anything in your Tidepool account, and it never controls a pump, a sensor or any other device.
- It is **not a medical device**. Do not use it, or anything built on it, for treatment decisions. Data can be delayed, incomplete or wrong. Rely on your own devices, apps and care team.
- It gives **no dosing advice and does no dosing calculations**. Insulin on board and carbs on board are shown only as the pump or app recorded them "as of the last dosing decision". Pump settings are shown as last uploaded. Nothing is estimated or predicted.
- It is **not affiliated with Tidepool** (see above).

## Features

- Latest continuous glucose reading, and its age in minutes.
- Glucose summaries (time in range, time below and above range, average, GMI, variability and more) for 24 hours, 7, 14 and 30 days, from Tidepool's own statistics.
- Pump values: basal rate and mode, last bolus, basal, bolus and total insulin of the last 24 hours, carbs, and whether automated delivery is active or delivery is suspended.
- Times of the last alarm, reservoir change, site change and delivery status change, and of the newest record of each data type.
- Connection state of the data sources Tidepool imports from.
- Event entities that fire once per new alarm, meal and bolus record.
- Optional entities (off by default) for pump and CGM settings, the bolus calculator's last entry, reported states and water intake.
- Several accounts per sign-in: your own, and accounts that share their data with you.
- A service to refresh now, and a service to import past glucose into Home Assistant's long-term statistics.
- Redacted, counts-only diagnostics and a repair issue if Tidepool withdraws the sign-in.

## Requirements

- Home Assistant 2026.10.0 or newer.
- A Tidepool account that signs in with an email address and password and does **not** use two-factor authentication (see [Sign-in and limitations](#sign-in-and-limitations)).
- Home Assistant must be able to reach `https://api.tidepool.org`.
- The `recorder` integration, only for the `tidepool.import_history` service. It is part of Home Assistant's default setup.

The integration needs no extra Python packages.

## Installation

### HACS (custom repository)

1. In Home Assistant, open HACS, open the menu at the top right and choose **Custom repositories**.
2. Add `https://github.com/ceb3/ha-tidepool` with the category **Integration**.
3. Find **Tidepool** in HACS, download it, and restart Home Assistant.

### Manual

1. Download the `custom_components/tidepool` folder from this repository (or `tidepool.zip` from a [release](https://github.com/ceb3/ha-tidepool/releases)).
2. Copy it to `custom_components/tidepool` in your Home Assistant configuration folder, so that `custom_components/tidepool/manifest.json` exists.
3. Restart Home Assistant.

## Setup

1. Go to **Settings > Devices & services > Add integration** and choose **Tidepool**.
2. Read the notice on the form, then enter the email address and password of your Tidepool account.
3. Choose which accounts to add. Your own account is selected by default; accounts that share their data with you are listed as well.
4. Finish. A device is created for each chosen account, with the entities described below.

To change the options later, open the Tidepool entry and choose **Configure**:

- **Accounts**: which accounts to show.
- **Glucose refresh interval**: how often to fetch the latest readings, from 2 to 30 minutes (default 5).

The entry reloads when the options change.

## Sign-in and limitations

Tidepool is added with your email address and password. This is the only sign-in the integration supports. The form shows this notice, which is repeated here:

> This method signs in with your Tidepool email and password using the login that Tidepool documents for developers. Tidepool recommends OAuth for apps, may change or restrict this login at any time without notice, and it will not work with accounts that require two-factor authentication. Your password is stored in Home Assistant's config storage so the integration can sign in again.

What this means in practice:

- Tidepool documents this login for developers. It may change or stop working at any time, without notice.
- Accounts with two-factor authentication will not work.
- Your password is stored in Home Assistant's config entry storage so the integration can sign in again. Anyone who can read your Home Assistant storage or backups can read it. Use a dedicated, strong password for the Tidepool account, do not reuse it elsewhere, and keep your backups encrypted.
- If Tidepool rejects the password (for example after you changed it), Home Assistant asks you to sign in again. To change the email or password yourself, use **Reconfigure** in the entry's menu. Both only accept the same Tidepool account the entry was set up with; to use another account, add another entry.
- If Tidepool withdraws or changes the sign-in, the integration cannot refresh. It raises the repair issue **Tidepool sign-in is no longer available** (**Settings > System > Repairs**), which links to this section. Signing in again will not help in that case. Home Assistant keeps trying, and the issue goes away by itself when the sign-in works again.

## Entities

Entities are grouped by the device they appear on. The patient device is named after the profile name of the Tidepool account (or "Tidepool patient" if it has none). Below it, one device is registered for each device that produced the account's latest records, named after the model in its data sets, or "Insulin pump", "CGM" or "Data source" when the model is unknown. A device that produced pump records is the pump, even if it also reads glucose. Pump entities and CGM entities sit on that pump or CGM device once one is known, and on the patient device until then.

Entity ids start with the patient's device name. In the tables, **Key** is the entity's translation key and **Default** says whether the entity is enabled when it is first created. Enable the ones that are off in **Settings > Devices & services > Entities** (switch on "Show disabled entities"), or from the entity's settings. Unique ids are stable and never contain the patient's name.

Glucose values use mg/dL if Home Assistant's unit system is US customary, and mmol/L otherwise (Tidepool stores mmol/L). Change the unit system under **Settings > System > General**. Where a table says "glucose unit", it means this.

A state is unknown when Tidepool has no matching data, or when the value cannot be read from the record.

### Patient device

Summaries come from Tidepool's own statistics for continuous glucose, which Tidepool recomputes about hourly, so they can be an hour behind. The ranges are the ones Tidepool uses; they cannot be changed here. Each statistic exists for four periods: 24 h, 7 d, 14 d and 30 d, as separate entities named like "Time in range (24 h)". Only some periods are on by default, as listed.

| Key | Name | Unit | Default (periods that are on) |
| --- | --- | --- | --- |
| `summary_time_in_range` | Time in range | % | 24 h, 14 d |
| `summary_low` | Time below range | % | 24 h |
| `summary_very_low` | Time very low | % | none |
| `summary_high` | Time above range | % | 24 h |
| `summary_very_high` | Time very high | % | none |
| `summary_extreme_high` | Time extremely high | % | none |
| `summary_average` | Average glucose | glucose unit | 14 d |
| `summary_gmi` | GMI | % | 14 d |
| `summary_cv` | Glucose variability, CV | % | 14 d |
| `summary_sd` | Glucose standard deviation | glucose unit | none |
| `summary_min` | Lowest glucose | glucose unit | none |
| `summary_max` | Highest glucose | glucose unit | none |
| `summary_hours_with_data` | Hours with data (diagnostic) | h | none |
| `summary_days_with_data` | Days with data (diagnostic) | d | none |

Diagnostic timestamps (time of the newest record of a data type, or of the newest upload):

| Key | Name | Type | Default |
| --- | --- | --- | --- |
| `last_data_cbg` | Last CGM data | timestamp | On |
| `last_data_basal` | Last basal data | timestamp | On |
| `last_data_bolus` | Last bolus data | timestamp | On |
| `last_data_smbg` | Last fingerstick data | timestamp | Off |
| `last_data_food` | Last food data | timestamp | Off |
| `last_data_insulin` | Last injection data | timestamp | Off |
| `last_data_physical_activity` | Last activity data | timestamp | Off |
| `last_data_device_event` | Last device event | timestamp | Off |
| `last_data_pump_settings` | Last pump settings | timestamp | Off |
| `last_data_cgm_settings` | Last CGM settings | timestamp | Off |
| `last_data_wizard` | Last bolus calculator data | timestamp | Off |
| `last_data_upload` | Last upload record | timestamp | Off |
| `last_upload` | Last upload | timestamp | On |

Data sources are the services Tidepool imports data from (for example a cloud account of a glucose monitor). One diagnostic connectivity sensor is created for each data source that Tidepool lists, named after its provider. It is on when Tidepool reports the source as connected, off when it is disconnected or in error, and unknown otherwise. Its attributes carry the state and the times of the last import and of the latest data.

| Key | Name | Type | Default |
| --- | --- | --- | --- |
| `data_source` | (Provider) connection | connectivity (on/off) | On |

Other optional records, not tied to a pump or CGM:

| Key | Name | Unit / type | Default |
| --- | --- | --- | --- |
| `reported_state` | Last reported state | timestamp, with the reported states as an attribute | Off |
| `water_intake` | Last water intake | volume, in the unit it was recorded in | Off |

### CGM device

| Key | Name | Unit / type | Default |
| --- | --- | --- | --- |
| `glucose` | Glucose | glucose unit; the newest continuous glucose reading | On |
| `glucose_age` | Glucose age | min; minutes since that reading (diagnostic, updated every minute) | On |
| `alert_high` | High glucose alert level | glucose unit (diagnostic); as last uploaded, with whether it is enabled and its snooze time as attributes | Off |
| `alert_low` | Low glucose alert level | glucose unit (diagnostic); as `alert_high` | Off |

### Pump device

| Key | Name | Unit / type | Default |
| --- | --- | --- | --- |
| `basal_rate` | Basal rate | U/h; the rate of the current basal segment | On |
| `basal_mode` | Basal mode | automated, scheduled, suspended or temporary | On |
| `last_bolus` | Last bolus | U delivered by the newest bolus (programmed amounts of cancelled boluses are ignored) | On |
| `insulin_basal_24h` | Basal insulin (24 h) | U, rolling 24 hours as recorded | On |
| `insulin_bolus_24h` | Bolus insulin (24 h) | U, rolling 24 hours as recorded | On |
| `insulin_total_24h` | Total insulin (24 h) | U, basal plus bolus | On |
| `carbs_last` | Last carbs | g, of the newest food record that has carbs | On |
| `carbs_24h` | Carbs (24 h) | g, rolling 24 hours | On |
| `last_injection` | Last injection | timestamp; the dose is an attribute | On |
| `last_activity` | Last activity | timestamp | On |
| `iob_at_last_decision` | Insulin on board at last dosing decision | U, as recorded then; not a live value | On |
| `cob_at_last_decision` | Carbs on board at last dosing decision | g, as recorded then; not a live value | On |
| `automated_delivery_active` | Automated delivery | on/off | On |
| `delivery_suspended` | Delivery suspended | on/off | On |
| `last_alarm` | Last alarm | timestamp (diagnostic); the alarm type is an attribute | On |
| `last_reservoir_change` | Last reservoir change | timestamp (diagnostic) | On |
| `last_site_change` | Last site change | timestamp (diagnostic); a cannula prime counts as the site change | On |
| `last_status_change` | Last delivery status change | timestamp (diagnostic); status and reason as attributes | On |
| `last_tubing_prime` | Last tubing prime | timestamp (diagnostic) | Off |
| `alarm` | Alarm | event | On |
| `meal` | Meal | event | On |
| `bolus` | Bolus | event | On |
| `active_schedule` | Active basal schedule | text (diagnostic) | Off |
| `scheduled_basal_now` | Scheduled basal rate now | U/h (diagnostic) | Off |
| `carb_ratio_now` | Carb ratio setting now | g/U (diagnostic) | Off |
| `sensitivity_now` | Insulin sensitivity setting now | glucose unit per U (diagnostic) | Off |
| `target_low_now` | Target range low now | glucose unit (diagnostic) | Off |
| `target_high_now` | Target range high now | glucose unit (diagnostic) | Off |
| `max_basal` | Maximum basal rate setting | U/h (diagnostic) | Off |
| `max_bolus` | Maximum bolus setting | U (diagnostic) | Off |
| `insulin_model` | Insulin model | text (diagnostic) | Off |
| `last_wizard_entry` | Last bolus calculator entry | timestamp; the carbs and glucose that were entered, and the pump's own insulin on board, are attributes. The calculator's suggested dose is deliberately not read. | Off |

Notes on the pump entities:

- The basal rate, basal mode and the two delivery sensors are unknown once the newest basal segment ended more than two hours ago, instead of showing an old state as current. The attributes still say when the segment ran.
- The "now" settings pick the entry of the pump's schedule that is in force at the pump's local time, and are refreshed every minute. The pump's local time comes from the time zone in its settings record. If the record only has a fixed UTC offset, Home Assistant's time zone is used when it had that same offset at the time of the record (so daylight saving time is followed); otherwise the offset is kept as it is. The `zone_basis` attribute of these sensors says which applies: `named_zone`, `home_zone` or `fixed_offset`.
- The carb ratio, insulin sensitivity, target low and target high sensors carry the attributes `as_of` (when the settings were uploaded) and `note` ("Pump setting as last uploaded; not advice."). Nothing is calculated from any setting.
- Insulin on board and carbs on board come from the newest dosing decision or bolus calculator record that has them. The attributes say when ("as of") and from which record. They are never extrapolated to the present.
- The 24 hour totals are unknown until the pump has uploaded basal or bolus records in the last 24 hours.

### Update cadence

- **Fast refresh**: every 5 minutes by default, 2 to 30 minutes by option. Each fast refresh makes two requests per account: the latest record of each type, and the basal, bolus, food and device event records of the last 24 hours (plus 12 hours before, for a basal segment that began earlier). The glucose, pump, insulin, carbs, event and "last data" entities update from it.
- **Slow refresh**: once an hour, not configurable. Each slow refresh makes about nine requests per account: the summaries (continuous glucose and fingerstick), the newest alarm, reservoir change, status change and prime, the data sources, the data sets (uploads) and the profile. Summaries, data source sensors, device event times and the last upload update from it.
- Signing in again, if Tidepool ends the session, adds a login request. If Tidepool limits requests, the integration waits at least as long as Tidepool asks before the next refresh.
- Tidepool data can lag the device by several minutes: a reading reaches Tidepool through the device's app or uploader first. A short delay is normal and is not a problem of the integration.

## Services

Both services are available under **Developer tools > Actions**, and can be used in automations.

### `tidepool.refresh`

Fetches the latest data now (a fast and a slow refresh) instead of waiting for the next scheduled one.

| Field | Required | Description |
| --- | --- | --- |
| `entry` | no | The Tidepool entry to refresh. All signed-in entries if left empty. |

Use it sparingly: every call makes the requests listed above.

### `tidepool.import_history`

Adds hourly mean, lowest and highest glucose of past days to Home Assistant's long-term statistics, so that graphs reach back before the integration was installed.

| Field | Required | Description |
| --- | --- | --- |
| `entry` | no | The Tidepool entry to import. All signed-in entries if left empty. |
| `days` | no | How many days back to import, from 1 to 90 (default 14). |

- The statistic is `tidepool:<id>_glucose`, one per account, named "Tidepool glucose (<patient name>)" ("Tidepool patient" if the profile has no name), where `<id>` is the account's Tidepool id in lower case with other characters replaced by underscores. You can find it in **Developer tools > Statistics** and pick it in a statistics graph card.
- Only whole, finished hours are imported, in the Home Assistant glucose unit at the time of the import. If you change the unit system later, run the import again over the whole window so the hours are alike.
- It is idempotent: importing the same days again replaces those hours and does not add them twice. If a run stops (for example because Tidepool limits requests), the hours already written stay and you can run it again.
- Tidepool is read in requests of seven days each, oldest first.
- It needs the recorder. Without it, the service reports an error.
- This integration does not delete statistics it has written.

## Events

The **Alarm**, **Meal** and **Bolus** entities are Home Assistant event entities, usable as triggers in automations. Their event types are:

- Alarm: auto off, low insulin, low power, no delivery, no insulin, no power, occlusion, over limit, other. The attributes are `alarm_type` and `time`.
- Meal: `meal`. The attributes are `time` and, if the record has it, `carbohydrates`.
- Bolus: normal, extended, dual wave, automated, other. The attributes are `units`, `time` and `context`.

How they fire:

- Each record fires **once**, when it first shows up in the 24 hour window. A record uploaded late still fires, once, with its own time as an attribute.
- Nothing is replayed. The records already there when the entity is first created are only marked as seen, and after a restart or reload only records that arrived since then fire.
- At most **5** events fire per refresh, the newest ones; the others are marked as seen without firing, so a long gap in uploads does not flood your automations.
- If the 24 hour window could not be read at the very first refresh after the entity was created, up to 5 records that are already old may fire once when it is read.

## Repairs and diagnostics

**Repairs.** The issue **Tidepool sign-in is no longer available** means Tidepool's email and password login did not answer as expected (it may have been changed or withdrawn). See [Sign-in and limitations](#sign-in-and-limitations). It clears itself once a request works again, and is removed with the entry.

A rejected password is a different case: Home Assistant asks you to sign in again from the entry instead.

**Diagnostics.** To download them, open **Settings > Devices & services > Tidepool**, open the menu of the entry (the three dots) and choose **Download diagnostics**. The file contains counts and flags only: the integration version, the option keys and refresh interval, the number of accounts, for each account the age of the last update and of the newest record in coarse buckets (such as "under 15 min"), record type names, how many records are in each window, how many data sources and data sets there are, which summary periods and kinds of device event exist, and how many child devices there are per role. It contains no glucose values or other health data, ids, names, email addresses, device ids, serial numbers, exact times or credentials. Look through it before you attach it to an issue.

## Privacy

What is stored, and where (all in your Home Assistant, nothing elsewhere):

- Your email address and password, and the chosen account ids, in the config entry (`.storage/core.config_entries`). The password is stored as text, so it is part of your backups; see [Sign-in and limitations](#sign-in-and-limitations).
- The session token Tidepool returns at sign-in is kept in memory only.
- Entity states and attributes (glucose, insulin, carbs and so on) in the recorder database, like any other entity, and in long-term statistics if you use `tidepool.import_history`. They are subject to your recorder settings (`recorder` includes, excludes and purge).
- The device and entity registries hold the patient's profile name (as device name and in entity ids) and the models of the devices.
- If you use `tidepool.import_history`, the patient's profile name is also written into the name of the long-term statistic (in the recorder database).
- The event entities keep, in Home Assistant's restore state, the ids and times of the records they have already fired for, so that they do not fire twice after a restart.

What is sent, and to whom:

- Requests go to `https://api.tidepool.org` and nowhere else. They carry your email and password to sign in, then a session token, and the ids of the accounts whose data is read.
- Nothing is sent to the author of this integration or to any third party. There is no telemetry, analytics or crash reporting.
- The integration's log messages are written to avoid identifiers, tokens, emails and passwords. Home Assistant's own log messages about the entry (for example when its setup is retried) may include the entry title, which is the patient's full name. Look through logs and remove names before sharing them.

## Troubleshooting and FAQ

**An entity is unknown.** Tidepool has no matching data for it, or the record lacks the field. Check the matching "Last ... data" sensor (turn it on if needed) to see whether Tidepool has such records at all. Pump entities stay unknown for an account that has no pump data. The basal and delivery entities are also unknown when the newest basal segment ended more than two hours ago.

**The values are old or stale.** First look at Tidepool itself: the integration can only show what Tidepool has, and Tidepool data can lag the device by several minutes or more, depending on the app or uploader. **Glucose age** shows how old the newest reading is. Summaries refresh hourly. Run `tidepool.refresh` to fetch now. If the entities are unavailable, look at the repair issue and at the log.

**Why are entities unavailable instead of showing the last value?** When a refresh fails (Tidepool cannot be reached, the sign-in fails, requests are limited), the entities of that refresh become unavailable until a refresh works again: the last value is not shown as if it were current. While data is flowing, **Glucose age** shows how old the newest reading is. If only one of the hourly extra requests fails (data sources, data sets, profile, a device event or the fingerstick summary), the value from the previous hour is kept instead.

**It says my sign-in does not work / I use two-factor authentication.** Accounts with two-factor authentication cannot sign in this way; see [Sign-in and limitations](#sign-in-and-limitations). A wrong email or password shows "The email or password is not correct". If Tidepool's login does not answer as expected, the form says so and no entry is created.

**What about accounts shared with me, or a caregiver account?** Accounts that share their data with you are listed when you add the integration, or later in the options. If a sharing account later withdraws access (Tidepool answers 403), that account's entities become unavailable while the other accounts carry on. Sharing may also not include some optional data (data sources, data sets, profile, device events); the entities that depend on it stay unknown. If an account's profile cannot be read at setup, that account is skipped and a warning is logged.

**Why are so many entities disabled?** To keep Home Assistant tidy: summaries for every period, settings and rarely useful records are there for those who want them. Enable them as described under [Entities](#entities).

**Why do I see mg/dL (or mmol/L)?** Glucose follows Home Assistant's unit system, not a per-entity setting: US customary gives mg/dL, otherwise mmol/L. Change it under **Settings > System > General**; entities follow. Values already stored in the recorder or in imported statistics keep the unit they were stored in.

**Why does an entity move between devices?** A pump or CGM entity is first on the patient device. When the data shows which device produced the records, the entity moves to that device with its entity id and history kept.

**How do I remove it?** Open **Settings > Devices & services > Tidepool**, open the menu of the entry and choose **Delete**. That removes the entry, its devices and entities, and its repair issue. Old backups of Home Assistant still hold the stored password; change the password on Tidepool if that matters to you. Statistics written by `tidepool.import_history` remain in the recorder database. Then, if you installed with HACS, remove the repository there too.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md). Please report problems with the [issue form](https://github.com/ceb3/ha-tidepool/issues/new/choose), and do not paste health data into issues.

## Disclaimer and licence

This project is provided as is, without warranty. It is not a medical device and not a substitute for professional medical advice, diagnosis or treatment. It is independent of Tidepool and not affiliated with, endorsed by, or supported by Tidepool; Tidepool and its icon belong to their owners.

Licensed under the [Apache License 2.0](LICENSE).
