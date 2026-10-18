# SPDX-License-Identifier: Apache-2.0
# Derived from https://github.com/arjenbos/ha-postnl, Copyright (c) 2023 Arjen Bos; modified.
"""Consumer API client for the postnl integration."""

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

_LOGGER = logging.getLogger(__name__)


class PostNLJouwAPI:
    """Thin client for the upstream API."""

    base_url: str = "https://jouw.postnl.nl/track-and-trace/"

    mymail_url: str = "https://jouw.postnl.nl/services/serverdrivenui/api/MyMail/letter"

    # (connect, read) timeout for every call. requests has no session-level
    # default; without this a hanging PostNL server blocks an executor
    # thread — and with it the whole refresh — indefinitely.
    timeout: tuple[int, int] = (10, 60)

    # The MyMail endpoints reject requests that only carry the bearer token; the
    # app-identification headers must be present on letter and image calls alike.
    mymail_headers: dict[str, str] = {
        "api-version": "1.37.0",
        "os-version": "35",
        "app-platform": "Android",
        "app-version": "11.0.1",
        "content-type": "application/json",
        "device-token": "00000000-0000-0000-0000-000000000000",
    }

    def __init__(self, access_token: str):
        """Initialize the PostNLJouwAPI."""
        self.client = requests.Session()
        self.client.mount(
            prefix='https://',
            adapter=HTTPAdapter(
                max_retries=Retry(
                    total=5,
                    backoff_factor=3
                ),
                pool_maxsize=25,
                pool_block=True
            )
        )
        self.client.headers = {
            "Authorization": "Bearer " + access_token
        }

    def track_and_trace(self, key):
        """Return track & trace details for a shipment."""
        response = self.client.get(
            self.base_url + "/api/trackAndTrace/" + key + "?language=nl",
            timeout=self.timeout,
        )

        return response.json()

    def letters(self):
        """Return the MyMail letters overview."""
        response = self.client.get(
            self.mymail_url, headers=self.mymail_headers, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def image(self, url: str) -> tuple[bytes, str]:
        """Fetch the raw bytes of a letter image, requiring the same auth as letters()."""
        response = self.client.get(url, headers=self.mymail_headers, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg")
        return response.content, content_type
