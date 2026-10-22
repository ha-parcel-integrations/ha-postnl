# SPDX-License-Identifier: Apache-2.0
# Derived from https://github.com/arjenbos/ha-postnl, Copyright (c) 2023 Arjen Bos; modified.
"""Login/OAuth API client for the postnl integration."""

import logging

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3 import Retry

_LOGGER = logging.getLogger(__name__)


class PostNLLoginAPI:
    """Thin client for the upstream API."""

    base_url: str = "https://login.postnl.nl/101112a0-4a0f-4bbb-8176-2f1b2d370d7c/"

    def __init__(self, access_token: str):
        """Initialize the PostNLLoginAPI."""
        self.client = requests.Session()
        self.client.mount(
            prefix="https://",
            adapter=HTTPAdapter(max_retries=Retry(total=5, backoff_factor=3)),
        )
        self.client.headers = CaseInsensitiveDict(
            {"Authorization": "Bearer " + access_token}
        )

    def userinfo(self):
        """Return the authenticated user's info."""
        response = self.client.get(self.base_url + "/profiles/oidc/userinfo")

        return response.json()
