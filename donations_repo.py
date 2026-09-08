"""Repository data access layer for the Donations DynamoDB table.

Provides atomic, conditional operations for creating, querying, claiming,
and updating surplus food donation records.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
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
    CoordinatorNotificationStatus,
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
            if client is not None and type(client).__module__.startswith("botocore"):
                import boto3

                self._client: Any = boto3.client(
                    "dynamodb", region_name=self._config.aws_region
                )
            else:
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
        item["quantity_kg"] = Decimal(str(item["quantity_kg"]))
        item["perishability_hours"] = Decimal(str(item["perishability_hours"]))
        if "donor_coordinates" in item and item["donor_coordinates"]:
            item["donor_coordinates"]["latitude"] = Decimal(
                str(item["donor_coordinates"]["latitude"])
            )
            item["donor_coordinates"]["longitude"] = Decimal(
                str(item["donor_coordinates"]["longitude"])
            )
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
                        "(attribute_not_exists(matched_recipient_id) "
                        "OR matched_recipient_id = :null_val) AND "
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
                        ":null_val": {"NULL": True},
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._config.recipients_table_name,
                    "Key": {"recipient_id": {"S": recipient_id}},
                    "UpdateExpression": (
                        "SET #cap = #cap - :qty"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(recipient_id) AND "
                        "#cap >= :qty"
                    ),
                    "ExpressionAttributeNames": {
                        "#cap": "capacity_kg_remaining",
                    },
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
                        "SET #cap = #cap + :qty"
                    ),
                    "ConditionExpression": "attribute_exists(recipient_id)",
                    "ExpressionAttributeNames": {
                        "#cap": "capacity_kg_remaining",
                    },
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

    @with_dynamodb_retry
    def query_unmatched_donations_by_region(
        self,
        service_region: str,
        limit: int | None = None,
        max_evaluated_items: int | None = None,
    ) -> list[Donation]:
        """Query unmatched (REPORTED) donations for a specific service region.

        Attempts to query the GSI 'status-ready_by-index' with status='reported'
        and filter on service_region. If the GSI does not exist or raises
        ValidationException, executes a bounded pagination scan fallback over
        the table (up to max_evaluated_items items) to prevent missing matching
        records when DynamoDB limits evaluate before filtering.

        Args:
            service_region: Operational target region.
            limit: Maximum number of matched donations to return.
            max_evaluated_items: Maximum items to evaluate in fallback scan.

        Returns:
            List of parsed Donation models in REPORTED status.
        """
        effective_limit = limit or self._config.max_unmatched_query_limit
        effective_max_evaluated = (
            max_evaluated_items or self._config.max_fallback_scan_evaluated_items
        )
        canonical_status = DonationStatus.REPORTED.value

        try:
            from boto3.dynamodb.conditions import Attr

            response = self._table.query(
                IndexName="status-ready_by-index",
                KeyConditionExpression=Key("status").eq(canonical_status),
                FilterExpression=Attr("service_region").eq(service_region),
                Limit=effective_limit,
            )
            items = response.get("Items", [])
            return [Donation.model_validate(item) for item in items]
        except ClientError as exc:
            err_msg = exc.response.get("Error", {}).get("Message", "")
            code = exc.response.get("Error", {}).get("Code", "")
            if (
                "The table does not have the specified index" in err_msg
                or code == "ValidationException"
            ):
                LOGGER.info(
                    "status-ready_by-index GSI unavailable, falling back to "
                    "bounded pagination scan for region %s",
                    service_region,
                )
                from boto3.dynamodb.conditions import Attr

                matched_donations: list[Donation] = []
                total_evaluated = 0
                last_evaluated_key = None

                while (
                    len(matched_donations) < effective_limit
                    and total_evaluated < effective_max_evaluated
                ):
                    scan_limit = min(
                        effective_limit - len(matched_donations),
                        effective_max_evaluated - total_evaluated,
                        50,
                    )
                    scan_kwargs: dict[str, Any] = {
                        "FilterExpression": (
                            Attr("service_region").eq(service_region)
                            & Attr("status").eq(canonical_status)
                        ),
                        "Limit": max(scan_limit, 1),
                    }
                    if last_evaluated_key:
                        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

                    scan_resp = self._table.scan(**scan_kwargs)
                    evaluated_in_call = scan_resp.get("ScannedCount", 0)
                    total_evaluated += evaluated_in_call

                    for item in scan_resp.get("Items", []):
                        matched_donations.append(Donation.model_validate(item))
                        if len(matched_donations) >= effective_limit:
                            break

                    last_evaluated_key = scan_resp.get("LastEvaluatedKey")
                    if not last_evaluated_key:
                        break

                return matched_donations
            raise

    @with_dynamodb_retry
    def query_unmatched_donations(
        self,
        limit: int | None = None,
        status: DonationStatus = DonationStatus.REPORTED,
        service_region: str | None = None,
        max_evaluated_items: int | None = None,
    ) -> list[Donation]:
        """Query donations by status with optional region filter and scan fallback.

        Args:
            limit: Maximum number of donations to return.
            status: Target DonationStatus to query (default: REPORTED).
            service_region: Optional service region filter.
            max_evaluated_items: Maximum items to evaluate in fallback scan.

        Returns:
            List of parsed Donation models matching criteria.
        """
        effective_limit = limit or self._config.max_unmatched_query_limit
        effective_max_evaluated = (
            max_evaluated_items or self._config.max_fallback_scan_evaluated_items
        )
        canonical_status = (
            status.value if isinstance(status, DonationStatus) else str(status)
        )

        try:
            from boto3.dynamodb.conditions import Attr, Key

            kwargs: dict[str, Any] = {
                "IndexName": "status-ready_by-index",
                "KeyConditionExpression": Key("status").eq(canonical_status),
                "Limit": effective_limit,
            }
            if service_region:
                kwargs["FilterExpression"] = Attr("service_region").eq(service_region)

            response = self._table.query(**kwargs)
            items = response.get("Items", [])
            return [Donation.model_validate(item) for item in items]
        except ClientError as exc:
            err_msg = exc.response.get("Error", {}).get("Message", "")
            code = exc.response.get("Error", {}).get("Code", "")
            if (
                "The table does not have the specified index" in err_msg
                or code == "ValidationException"
            ):
                from boto3.dynamodb.conditions import Attr

                matched_donations: list[Donation] = []
                total_evaluated = 0
                last_evaluated_key = None

                filter_cond = Attr("status").eq(canonical_status)
                if service_region:
                    filter_cond = (
                        filter_cond & Attr("service_region").eq(service_region)
                    )

                while (
                    len(matched_donations) < effective_limit
                    and total_evaluated < effective_max_evaluated
                ):
                    scan_limit = min(
                        effective_limit - len(matched_donations),
                        effective_max_evaluated - total_evaluated,
                        50,
                    )
                    scan_kwargs: dict[str, Any] = {
                        "FilterExpression": filter_cond,
                        "Limit": max(scan_limit, 1),
                    }
                    if last_evaluated_key:
                        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

                    scan_resp = self._table.scan(**scan_kwargs)
                    evaluated_in_call = scan_resp.get("ScannedCount", 0)
                    total_evaluated += evaluated_in_call

                    for item in scan_resp.get("Items", []):
                        matched_donations.append(Donation.model_validate(item))
                        if len(matched_donations) >= effective_limit:
                            break

                    last_evaluated_key = scan_resp.get("LastEvaluatedKey")
                    if not last_evaluated_key:
                        break

                return matched_donations
            raise

    @with_dynamodb_retry
    def escalate_missed_window_transaction(
        self,
        donation_id: str,
        reason: EscalationReason,
        transition_time: datetime | None = None,
    ) -> bool:
        """Atomically transition donation to ESCALATED and record audit event.

        Uses DynamoDB TransactWriteItems to couple the status transition
        (reported -> escalated) with the creation of an immutable audit record
        under idempotency key '{donation_id}:missed_window_escalation'.

        ConditionExpression strictly validates single canonical '#st = :reported'
        status. If another concurrent execution or retry attempts to escalate
        or record the audit event, the transaction is cancelled and this method
        returns False cleanly as an idempotent NO-OP.

        Args:
            donation_id: Target donation identifier.
            reason: Validated EscalationReason enum value.
            transition_time: Optional datetime for deterministic timestamps.

        Returns:
            True if this execution won the transaction and committed state;
            False if another execution already transitioned state (safe NO-OP).

        Raises:
            ClientError: If an unexpected DynamoDB error occurs.
        """
        now = transition_time or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        date_status = compute_date_status(DonationStatus.ESCALATED, now)
        idempotency_key = f"{donation_id}:missed_window_escalation"
        event_id = f"evt-{uuid.uuid4().hex[:12]}"

        target_key = {"donation_id": {"S": donation_id}}
        transact_items = [
            {
                "Update": {
                    "TableName": self._config.donations_table_name,
                    "Key": target_key,
                    "UpdateExpression": (
                        "SET #st = :escalated_status, #er = :reason, "
                        "#ua = :now, #ds = :date_status, #cns = :pending_status"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(donation_id) AND #st = :reported_status"
                    ),
                    "ExpressionAttributeNames": {
                        "#st": "status",
                        "#er": "escalation_reason",
                        "#ua": "updated_at",
                        "#ds": "date_status",
                        "#cns": "coordinator_notification_status",
                    },
                    "ExpressionAttributeValues": {
                        ":escalated_status": {"S": DonationStatus.ESCALATED.value},
                        ":reported_status": {"S": DonationStatus.REPORTED.value},
                        ":reason": {"S": reason.value},
                        ":now": {"S": now_iso},
                        ":date_status": {"S": date_status},
                        ":pending_status": {
                            "S": CoordinatorNotificationStatus.PENDING.value
                        },
                    },
                }
            },
            {
                "Put": {
                    "TableName": self._config.matches_audit_table_name,
                    "Item": {
                        "idempotency_key": {"S": idempotency_key},
                        "event_id": {"S": event_id},
                        "donation_id": {"S": donation_id},
                        "action": {"S": "ESCALATED_MISSED_WINDOW"},
                        "actor": {"S": "eventbridge_time_window_monitor"},
                        "timestamp": {"S": now_iso},
                        "details": {
                            "M": {
                                "reason": {"S": reason.value},
                                "escalated_at": {"S": now_iso},
                                "notification_status": {
                                    "S": CoordinatorNotificationStatus.PENDING.value
                                },
                            }
                        },
                    },
                    "ConditionExpression": "attribute_not_exists(idempotency_key)",
                }
            },

        ]

        try:
            self._client.transact_write_items(
                ClientRequestToken=idempotency_key,
                TransactItems=transact_items,
            )
            LOGGER.info(
                "Atomic missed-window escalation transaction succeeded for "
                "donation %s",
                donation_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                code_0 = reasons[0].get("Code") if len(reasons) > 0 else None
                code_1 = reasons[1].get("Code") if len(reasons) > 1 else None
                if (
                    code_0 == "ConditionalCheckFailed"
                    or code_1 == "ConditionalCheckFailed"
                ):
                    LOGGER.info(
                        "Missed-window escalation conditional check failed for "
                        "donation %s (code_0: %s, code_1: %s); safe NO-OP",
                        donation_id,
                        code_0,
                        code_1,
                    )
                    return False
            if code == "IdempotentParameterMismatchException":
                LOGGER.info(
                    "Idempotent parameter mismatch for %s; returning False as NO-OP",
                    idempotency_key,
                )
                return False
            raise

    @with_dynamodb_retry
    def claim_coordinator_notification(
        self,
        donation_id: str,
        claim_id: str,
        claim_time: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> bool:
        """Atomically claim the coordinator notification lease for a donation.

        Enforces strict pre-dispatch mutual exclusion using a DynamoDB
        conditional update:
        - Condition passes if:
          1. coordinator_notification_status == PENDING, OR
          2. coordinator_notification_status == CLAIMED AND
             coordinator_notification_claimed_at <= lease_threshold
        - If condition passes, sets status='CLAIMED', claimed_at=now,
          claim_id=claim_id.
        - If condition fails (another worker holds active claim, or already
          DELIVERED/FAILED), returns False cleanly as an idempotent safe NO-OP.

        Args:
            donation_id: Target donation identifier.
            claim_id: Unique claim/worker attempt identifier.
            claim_time: Optional datetime for deterministic testing.
            lease_seconds: Optional lease duration in seconds (defaults from config).

        Returns:
            True if claim lease was acquired; False if not acquired (safe NO-OP).
        """
        now = claim_time or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        effective_lease = (
            lease_seconds
            if lease_seconds is not None
            else self._config.notification_claim_lease_seconds
        )
        expired_threshold_iso = (now - timedelta(seconds=effective_lease)).isoformat()

        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET #cns = :claimed_status, #ca = :now, "
                    "#cid = :claim_id, #ua = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND ("
                    "#cns = :pending_status OR ("
                    "#cns = :claimed_status AND attribute_exists(#ca) "
                    "AND #ca <= :expired_threshold"
                    "))"
                ),
                ExpressionAttributeNames={
                    "#cns": "coordinator_notification_status",
                    "#ca": "coordinator_notification_claimed_at",
                    "#cid": "coordinator_notification_claim_id",
                    "#ua": "updated_at",
                },
                ExpressionAttributeValues={
                    ":claimed_status": CoordinatorNotificationStatus.CLAIMED.value,
                    ":pending_status": CoordinatorNotificationStatus.PENDING.value,
                    ":claim_id": claim_id,
                    ":now": now_iso,
                    ":expired_threshold": expired_threshold_iso,
                },
            )
            LOGGER.info(
                "Acquired coordinator notification claim for %s (claim_id: %s)",
                donation_id,
                claim_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.info(
                    "Coordinator notification claim rejected for donation %s "
                    "(active claim held or already terminal); safe NO-OP",
                    donation_id,
                )
                return False
            raise

    @with_dynamodb_retry
    def mark_coordinator_notification_delivered(
        self,
        donation_id: str,
        claim_id: str,
        delivery_time: datetime | None = None,
    ) -> bool:
        """Atomically transition notification state from CLAIMED to DELIVERED.

        Condition requires:
        - attribute_exists(donation_id)
        - coordinator_notification_status == CLAIMED
        - coordinator_notification_claim_id == claim_id

        Clears claim attributes (#ca, #cid) and sets status='DELIVERED'.

        Args:
            donation_id: Target donation identifier.
            claim_id: Claim identifier matching the current active lease.
            delivery_time: Optional datetime for deterministic updates.

        Returns:
            True if transitioned to DELIVERED; False if conditional check failed.
        """
        now = delivery_time or datetime.now(timezone.utc)
        now_iso = now.isoformat()

        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET #cns = :delivered_status, #ua = :now REMOVE #ca, #cid"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "#cns = :claimed_status AND #cid = :claim_id"
                ),
                ExpressionAttributeNames={
                    "#cns": "coordinator_notification_status",
                    "#cid": "coordinator_notification_claim_id",
                    "#ca": "coordinator_notification_claimed_at",
                    "#ua": "updated_at",
                },
                ExpressionAttributeValues={
                    ":delivered_status": CoordinatorNotificationStatus.DELIVERED.value,
                    ":claimed_status": CoordinatorNotificationStatus.CLAIMED.value,
                    ":claim_id": claim_id,
                    ":now": now_iso,
                },
            )
            LOGGER.info(
                "Marked coordinator notification DELIVERED for donation %s",
                donation_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.info(
                    "Mark delivered condition failed for donation %s "
                    "(claim_id mismatch or non-claimed state); safe NO-OP",
                    donation_id,
                )
                return False
            raise

    @with_dynamodb_retry
    def mark_coordinator_notification_failed(
        self,
        donation_id: str,
        claim_id: str,
        error_detail: str,
        failure_time: datetime | None = None,
    ) -> bool:
        """Atomically transition notification state from CLAIMED to FAILED.

        Used when delivery fails permanently (e.g. non-retryable 4xx client error).

        Args:
            donation_id: Target donation identifier.
            claim_id: Claim identifier matching current active lease.
            error_detail: Sanitized error description (no PII or raw dumps).
            failure_time: Optional datetime for deterministic updates.

        Returns:
            True if transitioned to FAILED; False if conditional check failed.
        """
        now = failure_time or datetime.now(timezone.utc)
        now_iso = now.isoformat()

        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET #cns = :failed_status, #ua = :now, "
                    "#err = :error_detail REMOVE #ca, #cid"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "#cns = :claimed_status AND #cid = :claim_id"
                ),
                ExpressionAttributeNames={
                    "#cns": "coordinator_notification_status",
                    "#cid": "coordinator_notification_claim_id",
                    "#ca": "coordinator_notification_claimed_at",
                    "#err": "coordinator_notification_error",
                    "#ua": "updated_at",
                },
                ExpressionAttributeValues={
                    ":failed_status": CoordinatorNotificationStatus.FAILED.value,
                    ":claimed_status": CoordinatorNotificationStatus.CLAIMED.value,
                    ":claim_id": claim_id,
                    ":error_detail": error_detail,
                    ":now": now_iso,
                },
            )
            LOGGER.warning(
                "Marked coordinator notification FAILED for donation %s: %s",
                donation_id,
                error_detail,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.info(
                    "Mark failed condition failed for donation %s; safe NO-OP",
                    donation_id,
                )
                return False
            raise

    @with_dynamodb_retry
    def release_coordinator_notification_claim(
        self,
        donation_id: str,
        claim_id: str,
    ) -> bool:
        """Release an active notification claim back to PENDING.

        Only invoked on clean recovery paths when a failure is definitively
        known to have occurred before downstream transmission was accepted.
        Resets status to PENDING and removes claim identifiers.

        Args:
            donation_id: Target donation identifier.
            claim_id: Claim identifier matching current active lease.

        Returns:
            True if claim was released; False if conditional check failed.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._table.update_item(
                Key={"donation_id": donation_id},
                UpdateExpression=(
                    "SET #cns = :pending_status, #ua = :now REMOVE #ca, #cid"
                ),
                ConditionExpression=(
                    "attribute_exists(donation_id) AND "
                    "#cns = :claimed_status AND #cid = :claim_id"
                ),
                ExpressionAttributeNames={
                    "#cns": "coordinator_notification_status",
                    "#cid": "coordinator_notification_claim_id",
                    "#ca": "coordinator_notification_claimed_at",
                    "#ua": "updated_at",
                },
                ExpressionAttributeValues={
                    ":pending_status": CoordinatorNotificationStatus.PENDING.value,
                    ":claimed_status": CoordinatorNotificationStatus.CLAIMED.value,
                    ":claim_id": claim_id,
                    ":now": now_iso,
                },
            )
            LOGGER.info(
                "Released notification claim for %s back to PENDING",
                donation_id,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.info(
                    "Release claim condition failed for donation %s; safe NO-OP",
                    donation_id,
                )
                return False
            raise

    @with_dynamodb_retry
    def query_pending_coordinator_notifications(
        self,
        service_region: str | None = None,
        limit: int | None = None,
        max_evaluated_items: int | None = None,
    ) -> list[Donation]:
        """Query donations in ESCALATED status needing coordinator notification.

        Retrieves records whose notification status is either PENDING or CLAIMED
        (allowing expired leases to be discovered and reclaimed).
        Queries GSI 'status-ready_by-index' with status='escalated' and filters on
        coordinator_notification_status in ('PENDING', 'CLAIMED').
        Falls back to bounded pagination scan if GSI is missing/invalid.

        Args:
            service_region: Optional regional filter.
            limit: Maximum items to return.
            max_evaluated_items: Maximum items to evaluate in fallback scan.

        Returns:
            List of parsed Donation models eligible for notification recovery.
        """
        effective_limit = limit or self._config.max_unmatched_query_limit
        effective_max_evaluated = (
            max_evaluated_items or self._config.max_fallback_scan_evaluated_items
        )
        canonical_status = DonationStatus.ESCALATED.value
        eligible_statuses = {
            CoordinatorNotificationStatus.PENDING.value,
            CoordinatorNotificationStatus.CLAIMED.value,
        }

        try:
            from boto3.dynamodb.conditions import Attr

            filter_expr = Attr("coordinator_notification_status").is_in(
                list(eligible_statuses)
            )
            if service_region:
                filter_expr = filter_expr & Attr("service_region").eq(service_region)

            response = self._table.query(
                IndexName="status-ready_by-index",
                KeyConditionExpression=Key("status").eq(canonical_status),
                FilterExpression=filter_expr,
                Limit=effective_limit,
            )
            raw_items = response.get("Items", [])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ValidationException":
                LOGGER.warning(
                    "GSI status-ready_by-index unavailable; falling back to scan "
                    "for pending coordinator alerts",
                    extra={"error": code},
                )

                from boto3.dynamodb.conditions import Attr

                scan_filter = Attr("status").eq(canonical_status) & Attr(
                    "coordinator_notification_status"
                ).is_in(list(eligible_statuses))
                if service_region:
                    scan_filter = scan_filter & Attr("service_region").eq(
                        service_region
                    )

                raw_items = []
                evaluated_count = 0
                last_key = None

                while (
                    len(raw_items) < effective_limit
                    and evaluated_count < effective_max_evaluated
                ):
                    scan_kwargs: dict[str, Any] = {
                        "FilterExpression": scan_filter,
                        "Limit": min(50, effective_limit - len(raw_items)),
                    }
                    if last_key:
                        scan_kwargs["ExclusiveStartKey"] = last_key

                    scan_resp = self._table.scan(**scan_kwargs)
                    page_items = scan_resp.get("Items", [])
                    raw_items.extend(page_items)
                    evaluated_count += scan_resp.get("ScannedCount", len(page_items))
                    last_key = scan_resp.get("LastEvaluatedKey")
                    if not last_key:
                        break
            else:
                raise

        # Post-filter in Python for exact matching (defends against basic mocks)
        results: list[Donation] = []
        for item in raw_items:
            cns = item.get("coordinator_notification_status")
            if cns in eligible_statuses:
                if service_region and item.get("service_region") != service_region:
                    continue
                results.append(Donation.model_validate(item))
                if len(results) >= effective_limit:
                    break

        return results


