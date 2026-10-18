# SPDX-License-Identifier: Apache-2.0
# Derived from https://github.com/arjenbos/ha-postnl, Copyright (c) 2023 Arjen Bos; modified.
"""GraphQL API client for the postnl integration."""

import logging

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport

_LOGGER = logging.getLogger(__name__)


class PostNLGraphql:
    """Thin client for the upstream API."""

    endpoint: str = "https://jouw.postnl.nl/account/api/graphql"
    client: Client

    def __init__(self, access_token: str):
        """Initialize the PostNLGraphql."""
        self.client = Client(transport=RequestsHTTPTransport(
            url=self.endpoint,
            verify=True,
            retries=3,
            timeout=60,
            headers={
                'Authorization': 'Bearer ' + access_token
            }
        ))

    def call(self, query: str):
        """Execute the request and return the parsed response."""
        query = gql(query)

        return self.client.execute(query)

    def profile(self):
        """Return the authenticated user's profile."""
        _LOGGER.debug('Fetching profile')
        query = """
            query {
              profile {
                ...ProfileData
                __typename
              }
            }

            fragment ProfileData on Profile {
              username
              __typename
            }
        """

        result = self.call(query)

        return result

    def shipments(self):
        """Return the tracked shipments."""
        _LOGGER.debug('Fetching shipments')

        query = """
        query {
          trackedShipments {
            receiverShipments {
              ...shipment
              __typename
            }
            senderShipments {
              ...shipment
              __typename
            }
            __typename
          }
        }
        fragment shipment on TrackedShipmentResultType {
          key
          creationDateTime
          title
          barcode
          delivered
          deliveredTimeStamp
          deliveryWindowFrom
          deliveryWindowTo
          deliveryWindowType
          detailsUrl
          shipmentType
          receiverTitle
          deliveryAddressType
          sourceAccountId
          sourceDisplayName
          __typename
        }
        """

        result = self.call(query)

        return result
