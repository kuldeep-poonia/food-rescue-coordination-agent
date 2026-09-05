"""Repository data access layer for the Volunteers DynamoDB table.

Provides volunteer registration, availability querying by operational region,
and atomic volunteer assignment state transitions.
"""

import logging
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import AppConfig, load_app_configuration
from dynamodb_retry import with_dynamodb_retry
from models import Volunteer, VolunteerStatus
from redaction import sanitize_payload_for_logging

LOGGER: logging.Logger = logging.getLogger(__name__)


class VolunteerUnavailableError(Exception):
    """Raised when an assignment is attempted on an unavailable volunteer."""


class VolunteersRepository:
    """Encapsulates all DynamoDB operations for the Volunteer entity."""

    def __init__(
        self,
        dynamodb_resource: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize repository with optional injected DynamoDB resource.

        Args:
            dynamodb_resource: Optional pre-configured boto3 DynamoDB resource.
            config: Optional application configuration instance.
        """
        self._config: AppConfig = config or load_app_configuration()
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(
                self._config.volunteers_table_name
            )
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.volunteers_table_name)

    @with_dynamodb_retry
    def create_volunteer(self, volunteer: Volunteer) -> None:
        """Persist a new validated volunteer into DynamoDB.

        Args:
            volunteer: The validated Volunteer model.

        Raises:
            ClientError: If DynamoDB write fails after retries.
        """
        item = volunteer.model_dump(mode="json")
        item["max_capacity_kg"] = Decimal(str(item["max_capacity_kg"]))
        lat = Decimal(str(item["coordinates"]["latitude"]))
        lon = Decimal(str(item["coordinates"]["longitude"]))
        item["coordinates"]["latitude"] = lat
        item["coordinates"]["longitude"] = lon

        LOGGER.info(
            "Creating volunteer record",
            extra={"volunteer": sanitize_payload_for_logging(item)},
        )
        self._table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(volunteer_id)",
        )

    @with_dynamodb_retry
    def get_volunteer(
        self, volunteer_id: str, consistent_read: bool = True
    ) -> Volunteer | None:
        """Retrieve a volunteer record by identifier.

        Policy decision: Volunteer availability checks require strongly
        consistent reads (consistent_read=True) to prevent double assignment.
        Eventual consistency is reserved for analytical dashboard queries.

        Args:
            volunteer_id: The unique volunteer identifier.
            consistent_read: Enforce strongly consistent read (default: True).

        Returns:
            The parsed Volunteer model if found, otherwise None.

        Raises:
            ClientError: If DynamoDB read fails after retries.
        """
        response = self._table.get_item(
            Key={"volunteer_id": volunteer_id},
            ConsistentRead=consistent_read,
        )
        item = response.get("Item")
        if not item:
            return None
        return Volunteer.model_validate(item)

    @with_dynamodb_retry
    def query_available_volunteers_by_region(
        self, service_region: str
    ) -> list[Volunteer]:
        """Query all available volunteers in a designated service region.

        Args:
            service_region: Geographic operational area identifier.

        Returns:
            List of matching available Volunteer model instances.

        Raises:
            ClientError: If DynamoDB query fails after retries.
        """
        response = self._table.query(
            IndexName="region-status-index",
            KeyConditionExpression=(
                Key("service_region").eq(service_region)
                & Key("status").eq(VolunteerStatus.AVAILABLE.value)
            ),
        )
        items = response.get("Items", [])
        return [Volunteer.model_validate(item) for item in items]

    @with_dynamodb_retry
    def set_volunteer_availability(
        self, volunteer_id: str, is_available: bool
    ) -> bool:
        """Atomically update a volunteer's availability status.

        Args:
            volunteer_id: Target volunteer identifier.
            is_available: Target boolean availability status.

        Returns:
            True if status transition succeeded atomically.

        Raises:
            VolunteerUnavailableError: If volunteer is already in target state.
            ClientError: If DynamoDB update fails unexpectedly.
        """
        new_status = (
            VolunteerStatus.AVAILABLE.value
            if is_available
            else VolunteerStatus.UNAVAILABLE.value
        )
        expected_current_status = (
            VolunteerStatus.UNAVAILABLE.value
            if is_available
            else VolunteerStatus.AVAILABLE.value
        )
        try:
            self._table.update_item(
                Key={"volunteer_id": volunteer_id},
                UpdateExpression="SET #st = :new_state",
                ConditionExpression=(
                    "attribute_exists(volunteer_id) AND "
                    "#st = :expected_state"
                ),
                ExpressionAttributeNames={
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":new_state": new_status,
                    ":expected_state": expected_current_status,
                },
            )
            LOGGER.info(
                "Volunteer %s availability transitioned to %s",
                volunteer_id,
                new_status,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise VolunteerUnavailableError(
                    f"Volunteer {volunteer_id} availability state conflict"
                ) from exc
            raise
