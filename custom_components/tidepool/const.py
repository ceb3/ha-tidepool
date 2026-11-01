"""Constants for the Tidepool integration."""

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
