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
from idempotency import build_idempotency_key
from models import (
    Donation,
    DonationStatus,
    InfrastructureConsistencyError,
)
from models import (
    DonationStateConflictError as DonationStateConflictError,
)
from recipients_repo import InsufficientCapacityError
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
            meta = getattr(dynamodb_resource, "meta", None)
            client = getattr(meta, "client", None) if meta else None
            self._client: Any = client if client is not None else dynamodb_resource
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.donations_table_name)
            self._client = dynamo.meta.client

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
    def get_donation(
        self, donation_id: str, consistent_read: bool = True
    ) -> Donation | None:
        """Retrieve a donation record by its unique identifier.

        Policy decision: Matching-critical state verification requires strongly
        consistent reads (consistent_read=True) to prevent stale state races.
        Eventual consistency (consistent_read=False) is reserved for reporting.

        Args:
            donation_id: The unique donation identifier.
            consistent_read: Enforce strongly consistent read (default: True).

        Returns:
            The parsed Donation model if found, otherwise None.

        Raises:
            ClientError: If DynamoDB query fails after retries.
        """
        response = self._table.get_item(
            Key={"donation_id": donation_id},
            ConsistentRead=consistent_read,
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

    @with_dynamodb_retry
    def unclaim_donation(self, donation_id: str, recipient_id: str) -> bool:
        """Atomically roll back a donation from MATCHED to REPORTED status.

        Args:
            donation_id: Target donation identifier.
            recipient_id: Recipient identifier to verify ownership before rollback.

        Returns:
            True if donation unclaimed atomically.

        Raises:
            DonationStateConflictError: If donation was not in matched state for
                recipient.
            ClientError: If an unexpected DynamoDB error occurs.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET #st = :reported_status, "
                    "updated_at = :now "
                    "REMOVE matched_recipient_id"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "matched_recipient_id = :recipient_id AND "
                    "#st = :matched_status"
                ),
                ExpressionAttributeNames={
                    "#st": "status",
                },
                ExpressionAttributeValues={
                    ":recipient_id": recipient_id,
                    ":reported_status": DonationStatus.REPORTED.value,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":now": now_iso,
                },
            )
            LOGGER.info(
                "Donation %s successfully unclaimed from recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning(
                    "Unclaim conflict: Donation %s state mismatch for recipient %s",
                    donation_id,
                    recipient_id,
                )
                raise DonationStateConflictError(
                    f"Donation {donation_id} is not claimed by recipient {recipient_id}"
                ) from exc
            raise

    @with_dynamodb_retry
    def claim_and_deduct_recipient(
        self, donation_id: str, recipient_id: str, quantity_kg: float
    ) -> bool:
        """Atomically claim donation and deduct recipient capacity via transaction.

        Args:
            donation_id: Target donation identifier.
            recipient_id: Target recipient organization identifier.
            quantity_kg: Donation weight in kilograms to deduct.

        Returns:
            True if transaction committed atomically.

        Raises:
            DonationClaimConflictError: If donation claim condition failed.
            InsufficientCapacityError: If recipient capacity condition failed.
            ClientError: If an unexpected DynamoDB error occurs.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        token = build_idempotency_key(donation_id, "claim_and_deduct")
        try:
            self._client.transact_write_items(
                ClientRequestToken=token,
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._config.donations_table_name,
                            "Key": {"donation_id": {"S": donation_id}},
                            "UpdateExpression": (
                                "SET matched_recipient_id = :recipient_id, "
                                "#st = :matched_status, "
                                "updated_at = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(donation_id) AND "
                                "attribute_not_exists(matched_recipient_id) AND "
                                "#st = :reported_status"
                            ),
                            "ExpressionAttributeNames": {"#st": "status"},
                            "ExpressionAttributeValues": {
                                ":recipient_id": {"S": recipient_id},
                                ":matched_status": {
                                    "S": DonationStatus.MATCHED.value
                                },
                                ":reported_status": {
                                    "S": DonationStatus.REPORTED.value
                                },
                                ":now": {"S": now_iso},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._config.recipients_table_name,
                            "Key": {"recipient_id": {"S": recipient_id}},
                            "UpdateExpression": (
                                "SET capacity_kg_remaining = "
                                "capacity_kg_remaining - :qty"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(recipient_id) AND "
                                "capacity_kg_remaining >= :qty"
                            ),
                            "ExpressionAttributeValues": {
                                ":qty": {"N": str(quantity_kg)},
                            },
                        }
                    },
                ],
            )
            LOGGER.info(
                "Atomic claim and deduct succeeded for donation %s, recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                code_0 = reasons[0].get("Code") if len(reasons) > 0 else None
                code_1 = reasons[1].get("Code") if len(reasons) > 1 else None

                # Priority rule: Donation claim invariant takes precedence
                if code_0 == "ConditionalCheckFailed":
                    LOGGER.warning(
                        "Claim condition failed for donation %s", donation_id
                    )
                    raise DonationClaimConflictError(
                        f"Donation {donation_id} already claimed or unavailable"
                    ) from exc
                if code_1 == "ConditionalCheckFailed":
                    LOGGER.warning(
                        "Capacity condition failed for recipient %s (%.2f kg)",
                        recipient_id,
                        quantity_kg,
                    )
                    raise InsufficientCapacityError(
                        f"Recipient {recipient_id} lacks capacity for {quantity_kg}kg"
                    ) from exc
            raise

    @with_dynamodb_retry
    def unclaim_and_restore_recipient(
        self, donation_id: str, recipient_id: str, quantity_kg: float
    ) -> bool:
        """Atomically unclaim donation and restore recipient capacity via transaction.

        Args:
            donation_id: Target donation identifier.
            recipient_id: Target recipient organization identifier.
            quantity_kg: Donation weight in kilograms to restore.

        Returns:
            True if transaction committed atomically.

        Raises:
            DonationStateConflictError: If donation state condition failed.
            InfrastructureConsistencyError: If compensation transaction fails.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        token = build_idempotency_key(donation_id, "unclaim_and_restore")
        try:
            self._client.transact_write_items(
                ClientRequestToken=token,
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._config.donations_table_name,
                            "Key": {"donation_id": {"S": donation_id}},
                            "UpdateExpression": (
                                "SET #st = :reported_status, "
                                "updated_at = :now "
                                "REMOVE matched_recipient_id"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(donation_id) AND "
                                "matched_recipient_id = :recipient_id AND "
                                "#st = :matched_status"
                            ),
                            "ExpressionAttributeNames": {"#st": "status"},
                            "ExpressionAttributeValues": {
                                ":recipient_id": {"S": recipient_id},
                                ":reported_status": {
                                    "S": DonationStatus.REPORTED.value
                                },
                                ":matched_status": {
                                    "S": DonationStatus.MATCHED.value
                                },
                                ":now": {"S": now_iso},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._config.recipients_table_name,
                            "Key": {"recipient_id": {"S": recipient_id}},
                            "UpdateExpression": (
                                "SET capacity_kg_remaining = "
                                "capacity_kg_remaining + :qty"
                            ),
                            "ConditionExpression": "attribute_exists(recipient_id)",
                            "ExpressionAttributeValues": {
                                ":qty": {"N": str(quantity_kg)},
                            },
                        }
                    },
                ],
            )
            LOGGER.info(
                "Atomic unclaim and restore succeeded for donation %s, recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                code_0 = reasons[0].get("Code") if len(reasons) > 0 else None
                if code_0 == "ConditionalCheckFailed":
                    raise DonationStateConflictError(
                        f"Donation {donation_id} is not claimed by {recipient_id}"
                    ) from exc
            LOGGER.critical(
                "INFRASTRUCTURE_INCONSISTENCY: Atomic unwind failed for donation %s",
                donation_id,
                extra={"details": {"error": str(exc)}},
            )
            raise InfrastructureConsistencyError(
                f"Atomic unclaim and restore failed for donation {donation_id}"
            ) from exc
