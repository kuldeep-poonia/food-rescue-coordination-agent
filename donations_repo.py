"""Repository data access layer for the Donations DynamoDB table.

Provides atomic, conditional operations for creating, querying, claiming,
and updating surplus food donation records.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from config import AppConfig, load_app_configuration
from dynamodb_retry import with_dynamodb_retry
from models import Donation, DonationStatus
from redaction import sanitize_payload_for_logging

LOGGER: logging.Logger = logging.getLogger(__name__)


class DonationClaimConflictError(Exception):
    """Raised when a concurrent claim conflict is detected on a donation."""


class DonationsRepository:
    """Encapsulates all DynamoDB access patterns for the Donations entity."""

    def __init__(
        self,
        dynamodb_resource: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize repository with optional injected resource for testing.

        Args:
            dynamodb_resource: Optional pre-configured boto3 DynamoDB resource.
            config: Optional application configuration instance.
        """
        self._config: AppConfig = config or load_app_configuration()
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(
                self._config.donations_table_name
            )
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.donations_table_name)

    @with_dynamodb_retry
    def create_donation(self, donation: Donation) -> None:
        """Persist a new validated donation into DynamoDB.

        Args:
            donation: The validated Donation model instance.

        Raises:
            ClientError: If DynamoDB write fails after retries.
        """
        item = donation.model_dump(mode="json")
        LOGGER.info(
            "Creating donation record",
            extra={"donation": sanitize_payload_for_logging(item)},
        )
        self._table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(donation_id)",
        )

    @with_dynamodb_retry
    def get_donation(self, donation_id: str) -> Donation | None:
        """Retrieve a donation record by its unique identifier.

        Args:
            donation_id: The unique donation identifier.

        Returns:
            The parsed Donation model if found, otherwise None.

        Raises:
            ClientError: If DynamoDB query fails after retries.
        """
        response = self._table.get_item(
            Key={"donation_id": donation_id},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return Donation.model_validate(item)

    @with_dynamodb_retry
    def claim_donation(self, donation_id: str, recipient_id: str) -> bool:
        """Atomically claim a donation for a specific recipient organization.

        Enforces atomic conditional write to prevent double-claiming race conditions.

        Args:
            donation_id: Target donation identifier.
            recipient_id: Recipient organization attempting to claim.

        Returns:
            True if claim succeeded atomically.

        Raises:
            DonationClaimConflictError: If another recipient already claimed it.
            ClientError: If an unexpected DynamoDB error occurs.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET matched_recipient_id = :recipient_id, "
                    "#st = :matched_status, "
                    "updated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "attribute_not_exists(matched_recipient_id) AND "
                    "#st = :reported_status"
                ),
                ExpressionAttributeNames={
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":recipient_id": recipient_id,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":reported_status": DonationStatus.REPORTED.value,
                    ":now": now_iso,
                },
            )
            LOGGER.info(
                "Donation %s successfully claimed by recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning(
                    "Race condition conflict: Donation %s claim failed for %s",
                    donation_id,
                    recipient_id,
                )
                raise DonationClaimConflictError(
                    f"Donation {donation_id} has already been claimed or is unavailable"
                ) from exc
            raise

    @with_dynamodb_retry
    def assign_volunteer(self, donation_id: str, volunteer_id: str) -> bool:
        """Atomically assign a volunteer to transport the donation.

        Args:
            donation_id: Target donation identifier.
            volunteer_id: Assigned volunteer identifier.

        Returns:
            True if assignment succeeded atomically, False otherwise.

        Raises:
            ClientError: If DynamoDB update fails unexpectedly.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET assigned_volunteer_id = :volunteer_id, "
                    "#st = :assigned_status, "
                    "updated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "attribute_not_exists(assigned_volunteer_id) AND "
                    "#st = :matched_status"
                ),
                ExpressionAttributeNames={
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":volunteer_id": volunteer_id,
                    ":assigned_status": DonationStatus.ASSIGNED.value,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":now": now_iso,
                },
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise
