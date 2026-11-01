"""Constants for the Tidepool integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tidepool"

API_URL: Final = "https://api.tidepool.org"

# Tidepool stores glucose in mmol/L; multiply by this factor to get mg/dL.
GLUCOSE_FACTOR: Final = 18.0182

# Device name of a patient whose Tidepool profile has no name.
DEFAULT_DEVICE_NAME: Final = "Tidepool patient"

# Config entry data and options keys.
CONF_PATIENTS: Final = "patients"
CONF_FAST_INTERVAL: Final = "fast_interval"

# Fast refresh interval in minutes.
DEFAULT_FAST_INTERVAL: Final = 5
MIN_FAST_INTERVAL: Final = 2
MAX_FAST_INTERVAL: Final = 30

# Slow refresh interval (summaries, data sources and sets, profile).
SLOW_INTERVAL: Final = timedelta(hours=1)

# Services and their fields.
SERVICE_REFRESH: Final = "refresh"
SERVICE_IMPORT_HISTORY: Final = "import_history"
ATTR_ENTRY: Final = "entry"
ATTR_DAYS: Final = "days"

# History import: the default and longest window in days, and the span of one
# request (Tidepool has no pagination, so a request's size is bounded by it).
DEFAULT_IMPORT_DAYS: Final = 14
MAX_IMPORT_DAYS: Final = 90
IMPORT_CHUNK: Final = timedelta(days=7)

# Shown on the email and password form through the ``disclaimer`` placeholder.
PASSWORD_DISCLAIMER: Final = (
    "This method signs in with your Tidepool email and password using the login "
    "that Tidepool documents for developers. Tidepool recommends OAuth for apps, "
    "may change or restrict this login at any time without notice, and it will "
    "not work with accounts that require two-factor authentication. Your "
    "password is stored in Home Assistant's config storage so the integration "
    "can sign in again."
)
