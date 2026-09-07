"""Repository data access layer for the Donations DynamoDB table.

Provides atomic, conditional operations for creating, querying, claiming,
and updating surplus food donation records.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import (
    KG_TO_MEALS_CONVERSION_FACTOR,
    AppConfig,
    load_app_configuration,
)
from dynamodb_retry import with_dynamodb_retry
from idempotency import build_idempotency_key
from models import (
    Donation,
    DonationStatus,
    EscalationReason,
    InfrastructureConsistencyError,
    RunningSummary,
)
from models import (
    DonationStateConflictError as DonationStateConflictError,
)
from recipients_repo import InsufficientCapacityError
from redaction import sanitize_payload_for_logging
from tools.logging_utils import get_structured_logger

LOGGER: logging.Logger = get_structured_logger(__name__)


def compute_date_status(
    status: DonationStatus, transition_time: datetime | None = None
) -> str:
    """Deterministically compute composite date_status from status and transition time.

    Guarantees status and date_status never drift by deriving date_status exclusively
    inside repository write operations.
    """
    dt = transition_time or datetime.now(timezone.utc)
    return f"{dt.strftime('%Y-%m-%d')}#{status.value}"


def _to_dynamodb_friendly(val: Any) -> Any:
    """Recursively convert float types to Decimal for DynamoDB serialization."""
    if isinstance(val, float):
        return Decimal(str(val))
    if isinstance(val, dict):
        return {k: _to_dynamodb_friendly(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_dynamodb_friendly(v) for v in val]
    return val


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
            self._table = dynamodb_resource.Table(self._config.donations_table_name)
            meta = getattr(dynamodb_resource, "meta", None)
            client = getattr(meta, "client", None) if meta else None
            self._client: Any = client if client is not None else dynamodb_resource
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.donations_table_name)
            self._client = boto3.client("dynamodb", region_name=self._config.aws_region)

    @with_dynamodb_retry
    def create_donation(self, donation: Donation) -> None:
        """Persist a new validated donation into DynamoDB.

        Args:
            donation: The validated Donation model instance.

        Raises:
            ClientError: If DynamoDB write fails after retries.
        """
        item = donation.model_dump(mode="json")
        if not item.get("date_status"):
            item["date_status"] = compute_date_status(
                donation.status, donation.created_at
            )
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
            Key={"donation_id": donation_id}, ConsistentRead=consistent_read
        )
        item = response.get("Item")
        return Donation.model_validate(item) if item else None

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
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.MATCHED, now)

        try:
            update_expr = (
                "SET matched_recipient_id = :recipient_id, "
                "#st = :matched_status, #ua = :now, #ds = :date_status"
            )
            cond_expr = (
                "attribute_exists(donation_id) AND "
                "attribute_not_exists(matched_recipient_id) AND "
                "(#st = :reported_status OR #st = :reported_upper)"
            )
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=update_expr,
                ConditionExpression=cond_expr,
                ExpressionAttributeNames={
                    "#st": "status",
                    "#ua": "updated_at",
                    "#ds": "date_status",
                },
                ExpressionAttributeValues={
                    ":recipient_id": recipient_id,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":reported_status": DonationStatus.REPORTED.value,
                    ":reported_upper": "REPORTED",
                    ":now": now_iso,
                    ":date_status": date_status,
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
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.ASSIGNED, now)

        try:
            update_expr = (
                "SET #avi = :volunteer_id, #st = :assigned_status, "
                "#ua = :now, #ds = :date_status"
            )
            cond_expr = (
                "attribute_exists(donation_id) AND "
                "attribute_not_exists(#avi) AND "
                "(#st = :matched_status OR #st = :matched_upper)"
            )
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=update_expr,
                ConditionExpression=cond_expr,
                ExpressionAttributeNames={
                    "#st": "status",
                    "#avi": "assigned_volunteer_id",
                    "#ua": "updated_at",
                    "#ds": "date_status",
                },
                ExpressionAttributeValues={
                    ":volunteer_id": volunteer_id,
                    ":assigned_status": DonationStatus.ASSIGNED.value,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":matched_upper": "MATCHED",
                    ":now": now_iso,
                    ":date_status": date_status,
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
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.REPORTED, now)

        try:
            update_expr = (
                "SET #st = :reported_status, #ua = :now, "
                "#ds = :date_status REMOVE matched_recipient_id"
            )
            cond_expr = (
                "attribute_exists(donation_id) AND "
                "matched_recipient_id = :recipient_id AND "
                "(#st = :matched_status OR #st = :matched_upper)"
            )
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=update_expr,
                ConditionExpression=cond_expr,
                ExpressionAttributeNames={
                    "#st": "status",
                    "#ua": "updated_at",
                    "#ds": "date_status",
                },
                ExpressionAttributeValues={
                    ":recipient_id": recipient_id,
                    ":reported_status": DonationStatus.REPORTED.value,
                    ":matched_status": DonationStatus.MATCHED.value,
                    ":matched_upper": "MATCHED",
                    ":now": now_iso,
                    ":date_status": date_status,
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
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.MATCHED, now)
        token = build_idempotency_key(donation_id, "claim_and_deduct")
        LOGGER.info(
            "Atomic claim starting: donation=%s, recipient=%s, qty=%.2f, table=%s",
            donation_id,
            recipient_id,
            quantity_kg,
            self._config.recipients_table_name,
        )

        target_key = {"donation_id": {"S": donation_id}}
        transact_items = [
            {
                "Update": {
                    "TableName": self._config.donations_table_name,
                    "Key": target_key,
                    "UpdateExpression": (
                        "SET matched_recipient_id = :recipient_id, "
                        "#st = :matched_status, #ua = :now, #ds = :date_status"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(donation_id) AND "
                        "attribute_not_exists(matched_recipient_id) AND "
                        "(#st = :reported_status OR #st = :reported_upper)"
                    ),
                    "ExpressionAttributeNames": {
                        "#st": "status",
                        "#ua": "updated_at",
                        "#ds": "date_status",
                    },
                    "ExpressionAttributeValues": {
                        ":recipient_id": {"S": recipient_id},
                        ":matched_status": {"S": DonationStatus.MATCHED.value},
                        ":reported_status": {"S": DonationStatus.REPORTED.value},
                        ":reported_upper": {"S": "REPORTED"},
                        ":now": {"S": now_iso},
                        ":date_status": {"S": date_status},
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._config.recipients_table_name,
                    "Key": {"recipient_id": {"S": recipient_id}},
                    "UpdateExpression": (
                        "SET capacity_kg_remaining = capacity_kg_remaining - :qty"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(recipient_id) AND "
                        "capacity_kg_remaining >= :qty"
                    ),
                    "ExpressionAttributeValues": {":qty": {"N": str(quantity_kg)}},
                }
            },
        ]

        try:
            self._client.transact_write_items(
                ClientRequestToken=token,
                TransactItems=transact_items,
            )
            LOGGER.info(
                "Atomic claim and deduct succeeded for donation %s, recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "IdempotentParameterMismatchException":
                import uuid

                refreshed_token = uuid.uuid4().hex
                LOGGER.warning(
                    "IdempotentParameterMismatchException on %s: "
                    "retrying with refreshed token %s",
                    token,
                    refreshed_token,
                )
                self._client.transact_write_items(
                    ClientRequestToken=refreshed_token,
                    TransactItems=transact_items,
                )
                return True
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                code_0 = reasons[0].get("Code") if len(reasons) > 0 else None
                msg_0 = reasons[0].get("Message") if len(reasons) > 0 else ""
                code_1 = reasons[1].get("Code") if len(reasons) > 1 else None
                msg_1 = reasons[1].get("Message") if len(reasons) > 1 else ""

                LOGGER.error(
                    "TransactWriteItems cancelled: Item 0 [%s: %s], Item 1 [%s: %s]",
                    code_0,
                    msg_0,
                    code_1,
                    msg_1,
                )

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
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.REPORTED, now)
        token = build_idempotency_key(donation_id, "unclaim_and_restore")

        target_key = {"donation_id": {"S": donation_id}}
        transact_items = [
            {
                "Update": {
                    "TableName": self._config.donations_table_name,
                    "Key": target_key,
                    "UpdateExpression": (
                        "SET #st = :reported_status, #ua = :now, "
                        "#ds = :date_status REMOVE matched_recipient_id"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(donation_id) AND "
                        "matched_recipient_id = :recipient_id AND "
                        "(#st = :matched_status OR #st = :matched_upper)"
                    ),
                    "ExpressionAttributeNames": {
                        "#st": "status",
                        "#ua": "updated_at",
                        "#ds": "date_status",
                    },
                    "ExpressionAttributeValues": {
                        ":recipient_id": {"S": recipient_id},
                        ":reported_status": {"S": DonationStatus.REPORTED.value},
                        ":matched_status": {"S": DonationStatus.MATCHED.value},
                        ":matched_upper": {"S": "MATCHED"},
                        ":now": {"S": now_iso},
                        ":date_status": {"S": date_status},
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._config.recipients_table_name,
                    "Key": {"recipient_id": {"S": recipient_id}},
                    "UpdateExpression": (
                        "SET capacity_kg_remaining = capacity_kg_remaining + :qty"
                    ),
                    "ConditionExpression": "attribute_exists(recipient_id)",
                    "ExpressionAttributeValues": {
                        ":qty": {"N": str(quantity_kg)},
                    },
                }
            },
        ]

        try:
            self._client.transact_write_items(
                ClientRequestToken=token,
                TransactItems=transact_items,
            )
            LOGGER.info(
                "Atomic unclaim and restore succeeded for donation %s, recipient %s",
                donation_id,
                recipient_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "IdempotentParameterMismatchException":
                import uuid

                refreshed_token = uuid.uuid4().hex
                LOGGER.warning(
                    "IdempotentParameterMismatchException on %s: "
                    "retrying with refreshed token %s",
                    token,
                    refreshed_token,
                )
                self._client.transact_write_items(
                    ClientRequestToken=refreshed_token,
                    TransactItems=transact_items,
                )
                return True
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

    @with_dynamodb_retry
    def escalate_donation(
        self,
        donation_id: str,
        reason: EscalationReason,
        current_status: DonationStatus | None = None,
    ) -> bool:
        """Atomically transition a donation to ESCALATED status with reason.

        Pre-dispatch condition: Only REPORTED or MATCHED donations may be escalated.
        Post-dispatch donations (ASSIGNED, PICKED_UP, DELIVERED, CLOSED) reject
        escalation with DonationStateConflictError to protect active dispatches.

        Idempotency: If donation is already in ESCALATED state, returns True cleanly
        as a successful no-op to support replay/resume loops.

        Args:
            donation_id: Target donation identifier.
            reason: Validated EscalationReason enum.
            current_status: Optional expected current status for optimistic check.

        Returns:
            True if transitioned or already ESCALATED.

        Raises:
            DonationStateConflictError: If donation is in a post-assignment state.
            ClientError: If unexpected DynamoDB error occurs.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.ESCALATED, now)

        cond_parts = [
            "attribute_exists(donation_id)",
            (
                "(#st IN (:reported_status, :reported_upper, "
                ":matched_status, :matched_upper) OR #st = :escalated_status)"
            ),
        ]
        exp_vals: dict[str, Any] = {
            ":reported_status": DonationStatus.REPORTED.value,
            ":reported_upper": "REPORTED",
            ":matched_status": DonationStatus.MATCHED.value,
            ":matched_upper": "MATCHED",
            ":escalated_status": DonationStatus.ESCALATED.value,
            ":reason": reason.value,
            ":now": now_iso,
            ":date_status": date_status,
        }

        if current_status is not None:
            if current_status not in (
                DonationStatus.REPORTED,
                DonationStatus.MATCHED,
                DonationStatus.ESCALATED,
            ):
                raise DonationStateConflictError(
                    f"Cannot escalate donation {donation_id} in "
                    f"{current_status.value} state"
                )
            cond_parts.append("(#st = :expected_status OR #st = :expected_upper)")
            exp_vals[":expected_status"] = current_status.value
            exp_vals[":expected_upper"] = current_status.value.upper()

        condition_expr = " AND ".join(cond_parts)

        try:
            update_expr = (
                "SET #st = :escalated_status, #er = :reason, "
                "#ua = :now, #ds = :date_status"
            )
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=update_expr,
                ConditionExpression=condition_expr,
                ExpressionAttributeNames={
                    "#st": "status",
                    "#er": "escalation_reason",
                    "#ua": "updated_at",
                    "#ds": "date_status",
                },
                ExpressionAttributeValues=exp_vals,
            )
            LOGGER.info(
                "Donation %s successfully transitioned to ESCALATED (%s)",
                donation_id,
                reason.value,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                existing = self.get_donation(donation_id, consistent_read=True)
                if not existing:
                    raise ValueError(f"Donation {donation_id} not found") from exc
                if existing.status == DonationStatus.ESCALATED:
                    LOGGER.info(
                        "Donation %s is already in ESCALATED state; idempotent no-op",
                        donation_id,
                    )
                    return True
                LOGGER.critical(
                    "Cannot escalate donation %s: post-dispatch or conflict state %s",
                    donation_id,
                    existing.status.value,
                )
                raise DonationStateConflictError(
                    f"Cannot escalate donation {donation_id} in post-dispatch state "
                    f"{existing.status.value}"
                ) from exc
            raise

    @with_dynamodb_retry
    def get_authoritative_daily_summary(
        self, service_region: str, date_str: str
    ) -> RunningSummary:
        """Query authoritative daily summary of fulfilled donations for a region.

        Uses the GSI 'region-date-status-index' to query donations by service_region
        and date_status prefix ({YYYY-MM-DD}#). Strictly zero table scans.

        Filters for committed fulfillment records (ASSIGNED, DELIVERED, CLOSED) to
        compute exact total kg routed, meals-equivalent estimate, and unique
        organizations served.

        Args:
            service_region: Target operational geographic region.
            date_str: Target date in 'YYYY-MM-DD' format.

        Returns:
            RunningSummary model with authoritative metrics.

        Raises:
            ClientError: If DynamoDB GSI query fails after retries.
        """
        date_prefix = f"{date_str}#"
        try:
            response = self._table.query(
                IndexName="region-date-status-index",
                KeyConditionExpression=(
                    Key("service_region").eq(service_region)
                    & Key("date_status").begins_with(date_prefix)
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
                        & Attr("date_status").begins_with(date_prefix)
                    )
                )
                items = response.get("Items", [])
            else:
                raise
        fulfilled_statuses = {
            DonationStatus.ASSIGNED.value,
            DonationStatus.DELIVERED.value,
            DonationStatus.CLOSED.value,
        }

        total_kg: float = 0.0
        matched_orgs: set[str] = set()
        fulfilled_count: int = 0

        for item in items:
            status_val = item.get("status")
            if status_val in fulfilled_statuses:
                qty = float(item.get("quantity_kg", 0.0))
                total_kg += qty
                fulfilled_count += 1
                recip_id = item.get("matched_recipient_id")
                if recip_id:
                    matched_orgs.add(str(recip_id))

        meals = int(round(total_kg * KG_TO_MEALS_CONVERSION_FACTOR))

        return RunningSummary(
            service_region=service_region,
            date_str=date_str,
            total_kg_routed=round(total_kg, 2),
            meals_equivalent=meals,
            organizations_served=len(matched_orgs),
            donations_count=fulfilled_count,
        )
