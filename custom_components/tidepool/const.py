"""Constants for the Tidepool integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tidepool"

API_URL: Final = "https://api.tidepool.org"
AUTH_URL: Final = "https://auth.tidepool.org/realms/tidepool"

# Tidepool stores glucose in mmol/L; multiply by this factor to get mg/dL.
GLUCOSE_FACTOR: Final = 18.0182

# Header that carries the OAuth access token on API requests. UNVERIFIED: a live
# check must decide whether Tidepool accepts it here or as ``Authorization:
# Bearer``. Only ``auth.oauth_token_headers`` may use this constant.
OAUTH_TOKEN_HEADER: Final = "X-Tidepool-Session-Token"

# Device name of a patient whose Tidepool profile has no name.
DEFAULT_DEVICE_NAME: Final = "Tidepool patient"

# Config entry data and options keys.
CONF_AUTH_METHOD: Final = "auth_method"
CONF_PATIENTS: Final = "patients"
CONF_FAST_INTERVAL: Final = "fast_interval"

AUTH_METHOD_OAUTH: Final = "oauth"
AUTH_METHOD_LEGACY: Final = "legacy"

# Fast refresh interval in minutes.
DEFAULT_FAST_INTERVAL: Final = 5
MIN_FAST_INTERVAL: Final = 2
MAX_FAST_INTERVAL: Final = 30

# Slow refresh interval (summaries, data sources and sets, profile).
SLOW_INTERVAL: Final = timedelta(hours=1)

# Shown on the email and password form through the ``disclaimer`` placeholder.
LEGACY_DISCLAIMER: Final = (
    "This method uses a Tidepool endpoint that Tidepool does not document for "
    "third parties. Tidepool recommends OAuth, and says password login is "
    "limited to trusted partners. It works today but may be changed or removed "
    "at any time without notice, and it will not work with accounts that "
    "require two-factor authentication. Use OAuth when you can."
)
