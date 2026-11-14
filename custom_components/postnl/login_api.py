# SPDX-License-Identifier: Apache-2.0
# Derived from https://github.com/arjenbos/ha-postnl, Copyright (c) 2023 Arjen Bos; modified.
"""Login/OAuth API client for the postnl integration."""

import logging

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3 import Retry

from .const import USER_AGENT

_LOGGER = logging.getLogger(__name__)


class PostNLLoginAPI:
    """Thin client for the upstream API."""

    base_url: str = "https://login.postnl.nl/101112a0-4a0f-4bbb-8176-2f1b2d370d7c/"

    # (connect, read) timeout for every call. requests has no session-level
    # default, and this call runs inside async_setup_entry — without it a
    # hanging login server leaves setup waiting forever instead of failing
    # into HA's retry (#50).
    timeout: tuple[int, int] = (10, 30)

    def __init__(self, access_token: str):
        """Initialize the PostNLLoginAPI."""
        self.client = requests.Session()
        self.client.mount(
            prefix="https://",
            adapter=HTTPAdapter(max_retries=Retry(total=5, backoff_factor=3)),
        )
        self.client.headers = CaseInsensitiveDict(
            {
                "Authorization": "Bearer " + access_token,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

    def userinfo(self):
        """Return the authenticated user's info."""
        response = self.client.get(
            self.base_url + "/profiles/oidc/userinfo", timeout=self.timeout
        )

        return response.json()
