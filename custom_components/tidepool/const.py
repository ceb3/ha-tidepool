"""Constants for the Tidepool integration."""

from typing import Final

DOMAIN: Final = "tidepool"

API_URL: Final = "https://api.tidepool.org"
AUTH_URL: Final = "https://auth.tidepool.org/realms/tidepool"

# Tidepool stores glucose in mmol/L; multiply by this factor to get mg/dL.
GLUCOSE_FACTOR: Final = 18.0182
