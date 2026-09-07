"""Repository data access layer for the Recipients DynamoDB table.

Provides atomic capacity reservation, recipient queries by service region,
and capacity restoration upon cancellation.
"""

import logging
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import AppConfig, load_app_configuration
from dynamodb_retry import with_dynamodb_retry
from models import Recipient, RecipientStatus
from redaction import sanitize_payload_for_logging
from tools.logging_utils import get_structured_logger

LOGGER: logging.Logger = get_structured_logger(__name__)


class InsufficientCapacityError(Exception):
    """Raised when a recipient has insufficient capacity remaining."""


class RecipientsRepository:
    """Encapsulates all DynamoDB operations for the Recipient entity."""

    def __init__(
        self,
        dynamodb_resource: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize repository with optional injected resource.

        Args:
            dynamodb_resource: Optional pre-configured boto3 DynamoDB resource.
            config: Optional application configuration instance.
        """
        self._config: AppConfig = config or load_app_configuration()
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(self._config.recipients_table_name)
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.recipients_table_name)

    @with_dynamodb_retry
    def create_recipient(self, recipient: Recipient) -> None:
        """Persist a new validated recipient organization into DynamoDB.

        Args:
            recipient: The validated Recipient model.

        Raises:
            ClientError: If DynamoDB write fails after retries.
        """
        item = recipient.model_dump(mode="json")
        # Convert float capacity and coordinates to Decimal for DynamoDB storage
        item["capacity_kg_remaining"] = Decimal(str(item["capacity_kg_remaining"]))
        lat = Decimal(str(item["coordinates"]["latitude"]))
        lon = Decimal(str(item["coordinates"]["longitude"]))
        item["coordinates"]["latitude"] = lat
        item["coordinates"]["longitude"] = lon

        LOGGER.info(
            "Creating recipient record",
            extra={"recipient": sanitize_payload_for_logging(item)},
        )
        self._table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(recipient_id)",
        )

    @with_dynamodb_retry
    def get_recipient(
        self, recipient_id: str, consistent_read: bool = True
    ) -> Recipient | None:
        """Retrieve a recipient record by its unique identifier.

        Policy decision: Recipient capacity checks require strongly consistent
        reads (consistent_read=True) to prevent over-allocation during matches.
        Eventual consistency is reserved for analytical dashboard queries.

        Args:
            recipient_id: The unique recipient identifier.
            consistent_read: Enforce strongly consistent read (default: True).

        Returns:
            The parsed Recipient model if found, otherwise None.

        Raises:
            ClientError: If DynamoDB read fails after retries.
        """
        response = self._table.get_item(
            Key={"recipient_id": recipient_id},
            ConsistentRead=consistent_read,
        )
        item = response.get("Item")
        if not item:
            return None
        # Convert Decimal values back to float for Pydantic validation
        return Recipient.model_validate(item)

    @with_dynamodb_retry
    def query_active_recipients_by_region(self, service_region: str) -> list[Recipient]:
        """Query all active recipients in a designated service region.

        Args:
            service_region: Geographic operational area identifier.

        Returns:
            List of matching active Recipient model instances.

        Raises:
            ClientError: If DynamoDB query fails after retries.
        """
        try:
            response = self._table.query(
                IndexName="region-status-index",
                KeyConditionExpression=(
                    Key("service_region").eq(service_region)
                    & Key("status").eq(RecipientStatus.ACTIVE.value)
                ),
            )
            items = response.get("Items", [])
        except ClientError as exc:
            err_msg = exc.response.get("Error", {}).get("Message", "")
            code = exc.response.get("Error", {}).get("Code", "")
            if (
                "The table does not have the specified index" in err_msg
                or code == "ValidationException"
            ):
                from boto3.dynamodb.conditions import Attr

                response = self._table.scan(
                    FilterExpression=(
                        Attr("service_region").eq(service_region)
                        & Attr("status").eq(RecipientStatus.ACTIVE.value)
                    )
                )
                items = response.get("Items", [])
            else:
                raise
        return [Recipient.model_validate(item) for item in items]

    @with_dynamodb_retry
    def deduct_capacity(self, recipient_id: str, quantity_kg: float) -> bool:
        """Atomically deduct capacity if sufficient remaining capacity exists.

        Args:
            recipient_id: Target recipient organization identifier.
            quantity_kg: Weight in kilograms to deduct.

        Returns:
            True if deduction succeeded atomically.

        Raises:
            InsufficientCapacityError: If remaining capacity is below required weight.
            ClientError: If unexpected DynamoDB error occurs.
        """
        qty_decimal = Decimal(str(quantity_kg))
        try:
            self._table.update_item(
                Key={"recipient_id": recipient_id},
                UpdateExpression=(
                    "SET capacity_kg_remaining = capacity_kg_remaining - :qty"
                ),
                ConditionExpression=(
                    "attribute_exists(recipient_id) AND capacity_kg_remaining >= :qty"
                ),
                ExpressionAttributeValues={
                    ":qty": qty_decimal,
                },
            )
            LOGGER.info(
                "Deducted %.2f kg from recipient %s",
                quantity_kg,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning(
                    "Capacity deduction failed for %s: requested %.2f kg",
                    recipient_id,
                    quantity_kg,
                )
                raise InsufficientCapacityError(
                    f"Recipient {recipient_id} does not have {quantity_kg}kg capacity"
                ) from exc
            raise

    @with_dynamodb_retry
    def restore_capacity(self, recipient_id: str, quantity_kg: float) -> bool:
        """Atomically restore recipient capacity upon compensation or cancellation.

        Args:
            recipient_id: Target recipient organization identifier.
            quantity_kg: Weight in kilograms to restore.

        Returns:
            True if capacity restored atomically.

        Raises:
            KeyError: If recipient does not exist in the database.
            ClientError: If unexpected DynamoDB error occurs.
        """
        qty_decimal = Decimal(str(quantity_kg))
        try:
            self._table.update_item(
                Key={"recipient_id": recipient_id},
                UpdateExpression=(
                    "SET capacity_kg_remaining = capacity_kg_remaining + :qty"
                ),
                ConditionExpression="attribute_exists(recipient_id)",
                ExpressionAttributeValues={
                    ":qty": qty_decimal,
                },
            )
            LOGGER.info(
                "Restored %.2f kg to recipient %s",
                quantity_kg,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning(
                    "Capacity restore failed: recipient %s does not exist",
                    recipient_id,
                )
                raise KeyError(f"Recipient {recipient_id} does not exist") from exc
            raise

    @with_dynamodb_retry
    def update_recipient_capacity(
        self,
        recipient_id: str,
        capacity_kg_remaining: float,
        dietary_requirements: list[str] | None = None,
        dietary_exclusions: list[str] | None = None,
        status: RecipientStatus | None = None,
    ) -> bool:
        """Update recipient capacity and operational attributes.

        Args:
            recipient_id: Target recipient identifier.
            capacity_kg_remaining: New remaining capacity in kg.
            dietary_requirements: Optional updated dietary preferences.
            dietary_exclusions: Optional updated dietary exclusions.
            status: Optional operational status.

        Returns:
            True if update succeeded.

        Raises:
            KeyError: If recipient does not exist.
        """
        qty_decimal = Decimal(str(capacity_kg_remaining))
        set_clauses = ["capacity_kg_remaining = :cap"]
        expr_vals: dict[str, Any] = {":cap": qty_decimal}
        expr_names: dict[str, str] = {}

        if dietary_requirements is not None:
            set_clauses.append("dietary_requirements = :dreq")
            expr_vals[":dreq"] = dietary_requirements
        if dietary_exclusions is not None:
            set_clauses.append("dietary_exclusions = :dexc")
            expr_vals[":dexc"] = dietary_exclusions
        if status is not None:
            set_clauses.append("#st = :status")
            expr_vals[":status"] = status.value
            expr_names["#st"] = "status"

        update_expr = "SET " + ", ".join(set_clauses)
        params: dict[str, Any] = {
            "Key": {"recipient_id": recipient_id},
            "UpdateExpression": update_expr,
            "ConditionExpression": "attribute_exists(recipient_id)",
            "ExpressionAttributeValues": expr_vals,
        }
        if expr_names:
            params["ExpressionAttributeNames"] = expr_names

        try:
            self._table.update_item(**params)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise KeyError(f"Recipient {recipient_id} does not exist") from exc
            raise
