"""Repository data access layer for the Matches and AuditLog DynamoDB table.

Provides immutable audit event recording with idempotency guarantees
and chronological lifecycle retrieval for donations.
"""

import logging
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import AppConfig, load_app_configuration
from dynamodb_retry import with_dynamodb_retry
from models import AuditEvent
from redaction import sanitize_payload_for_logging

LOGGER: logging.Logger = logging.getLogger(__name__)


class AuditRepository:
    """Encapsulates all DynamoDB operations for the audit trail."""

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
                self._config.matches_audit_table_name
            )
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.matches_audit_table_name)

    @with_dynamodb_retry
    def record_audit_event(self, event: AuditEvent) -> bool:
        """Record an immutable audit event enforcing idempotency key uniqueness.

        Args:
            event: The validated AuditEvent model.

        Returns:
            True if event was persisted, False if duplicate idempotency key detected.
            CONSUMER CONTRACT: False indicates the event was already successfully
            persisted in a prior attempt/replay. Callers (tools, Lambda handlers,
            orchestrator) must treat False as a successful no-op, never retry or
            escalate.

        Raises:
            ClientError: If DynamoDB write fails for an unexpected reason.
        """
        item = event.model_dump(mode="json")
        LOGGER.info(
            "Recording audit event %s for donation %s",
            event.event_id,
            event.donation_id,
            extra={"details": sanitize_payload_for_logging(event.details)},
        )

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.warning(
                    "Idempotent duplicate ignored: key %s already recorded",
                    event.idempotency_key,
                )
                return False
            raise

    @with_dynamodb_retry
    def query_audit_trail_by_donation(self, donation_id: str) -> list[AuditEvent]:
        """Retrieve the complete chronological audit trail for a donation.

        Args:
            donation_id: Unique donation identifier.

        Returns:
            List of chronologically ordered AuditEvent model instances.

        Raises:
            ClientError: If DynamoDB query fails after retries.
        """
        response = self._table.query(
            IndexName="donation-audit-index",
            KeyConditionExpression=Key("donation_id").eq(donation_id),
            ScanIndexForward=True,
        )
        items = response.get("Items", [])
        return [AuditEvent.model_validate(item) for item in items]
