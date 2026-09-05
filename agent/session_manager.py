"""Session management layer for Food Rescue Coordination Agent.

Provides concurrency-safe session lifecycle, atomic counter and capacity
tracking using DynamoDB atomic operations, and periodic reconciliation against
authoritative fulfillment records.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from config import AppConfig, load_app_configuration
from donations_repo import DonationsRepository
from dynamodb_retry import with_dynamodb_retry
from models import InfrastructureConsistencyError, RunningSummary, SessionContext

LOGGER: logging.Logger = logging.getLogger(__name__)


class AgentSessionManager:
    """Manages ephemeral operational session state in DynamoDB."""

    def __init__(
        self,
        dynamodb_resource: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize session manager with optional injected resource and config.

        Args:
            dynamodb_resource: Optional pre-configured boto3 DynamoDB resource.
            config: Optional application configuration.
        """
        self._config: AppConfig = config or load_app_configuration()
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(
                self._config.sessions_memory_table_name
            )
        else:
            import boto3

            dynamo = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamo.Table(self._config.sessions_memory_table_name)

    @staticmethod
    def _build_keys(service_region: str, session_date: str) -> tuple[str, str]:
        """Format partition and sort keys for a session item.

        Args:
            service_region: Geographic operating region.
            session_date: Target date string in YYYY-MM-DD format.

        Returns:
            Tuple of (PK, SK).
        """
        return f"SESSION#{service_region}#{session_date}", "METADATA"

    @staticmethod
    def _format_date(date_str: str | None = None) -> str:
        """Return standardized YYYY-MM-DD date string."""
        if date_str:
            return date_str
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @with_dynamodb_retry
    def get_or_create_session(
        self,
        service_region: str,
        date_str: str | None = None,
    ) -> SessionContext:
        """Retrieve existing daily session or atomically initialize a new one.

        Concurrency-safe: Writes initial session with attribute_not_exists(PK).
        If a race occurs and another process creates the session simultaneously,
        catches ConditionalCheckFailedException and cleanly falls back to load_session.

        Args:
            service_region: Target geographic operational region.
            date_str: Optional operational date (defaults to current UTC date).

        Returns:
            Validated SessionContext domain model.

        Raises:
            InfrastructureConsistencyError: If session creation fails unexpectedly.
            ClientError: If unexpected DynamoDB error occurs.
        """
        session_date = self._format_date(date_str)
        pk, sk = self._build_keys(service_region, session_date)
        session_id = f"sess-{service_region}-{session_date}"

        # Fast path: try loading existing session
        existing = self.load_session(service_region, session_date, consistent_read=True)
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc)
        ttl = int(now.timestamp()) + (self._config.session_ttl_hours * 3600)
        now_iso = now.isoformat()

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "session_id": session_id,
            "service_region": service_region,
            "session_date": session_date,
            "recent_volunteer_assignments": {},
            "total_donations_processed": 0,
            "total_kg_routed": Decimal("0.0"),
            "active_escalations_count": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "ttl_epoch": ttl,
        }

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            LOGGER.info(
                "Initialized new session %s for region %s and date %s",
                session_id,
                service_region,
                session_date,
            )
            return SessionContext(
                session_id=session_id,
                service_region=service_region,
                session_date=session_date,
                recipients_near_capacity=[],
                recent_volunteer_assignments={},
                total_donations_processed=0,
                total_kg_routed=0.0,
                active_escalations_count=0,
                created_at=now,
                updated_at=now,
                ttl_epoch=ttl,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                LOGGER.info(
                    "Session %s created concurrently by another worker; loading",
                    session_id,
                )
                loaded = self.load_session(
                    service_region, session_date, consistent_read=True
                )
                if loaded is not None:
                    return loaded
                raise InfrastructureConsistencyError(
                    f"Concurrent creation race failed to load session {session_id}"
                ) from exc
            raise

    @with_dynamodb_retry
    def load_session(
        self,
        service_region: str,
        date_str: str | None = None,
        consistent_read: bool = True,
    ) -> SessionContext | None:
        """Load session context strongly consistent from DynamoDB.

        Args:
            service_region: Target geographic operational region.
            date_str: Optional operational date (defaults to current UTC date).
            consistent_read: Whether to perform strongly consistent read.

        Returns:
            Validated SessionContext or None if session does not exist.
        """
        session_date = self._format_date(date_str)
        pk, sk = self._build_keys(service_region, session_date)

        response = self._table.get_item(
            Key={"PK": pk, "SK": sk},
            ConsistentRead=consistent_read,
        )
        item = response.get("Item")
        if not item:
            return None

        return self._item_to_session_context(item)

    @with_dynamodb_retry
    def record_donation_outcome(
        self,
        service_region: str,
        quantity_kg: float,
        outcome: str,
        recipient_id: str | None = None,
        volunteer_id: str | None = None,
        near_capacity: bool = False,
        date_str: str | None = None,
    ) -> None:
        """Atomically record donation outcome counters and advisory signals.

        Concurrency-safe: Uses DynamoDB ADD expressions to increment counters
        and string sets for capacity flags, preventing lost updates under bursts.

        Args:
            service_region: Geographic operational region.
            quantity_kg: Quantity of food routed in kilograms.
            outcome: Donation lifecycle outcome ('matched', 'escalated', etc.).
            recipient_id: Optional matched recipient ID.
            volunteer_id: Optional assigned volunteer ID.
            near_capacity: True if remaining capacity < threshold.
            date_str: Optional target date.
        """
        session_date = self._format_date(date_str)
        pk, sk = self._build_keys(service_region, session_date)

        # Ensure session exists first
        self.get_or_create_session(service_region, session_date)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Build atomic ADD expressions
        add_clauses: list[str] = [
            "total_donations_processed :one",
            "total_kg_routed :qty",
        ]
        exp_vals: dict[str, Any] = {
            ":one": 1,
            ":zero": 0,
            ":qty": Decimal(str(round(quantity_kg, 2))),
            ":now": now_iso,
        }
        exp_names: dict[str, str] = {}

        if outcome == "escalated":
            add_clauses.append("active_escalations_count :one")

        delete_clauses: list[str] = []

        # Maintain advisory recipients_near_capacity set
        if recipient_id:
            if near_capacity:
                add_clauses.append("recipients_near_capacity :rec_set")
                exp_vals[":rec_set"] = {recipient_id}
            else:
                delete_clauses.append("recipients_near_capacity :del_rec_set")
                exp_vals[":del_rec_set"] = {recipient_id}

        set_clauses: list[str] = ["updated_at = :now"]

        # Maintain advisory volunteer workload distribution
        if volunteer_id:
            exp_names["#vol"] = volunteer_id
            set_clauses.append(
                "recent_volunteer_assignments.#vol = "
                "if_not_exists(recent_volunteer_assignments.#vol, :zero) + :one"
            )

        update_expr_parts: list[str] = []
        if set_clauses:
            update_expr_parts.append("SET " + ", ".join(set_clauses))
        if add_clauses:
            update_expr_parts.append("ADD " + ", ".join(add_clauses))
        if delete_clauses:
            update_expr_parts.append("DELETE " + ", ".join(delete_clauses))

        full_update_expr = " ".join(update_expr_parts)

        update_kwargs: dict[str, Any] = {
            "Key": {"PK": pk, "SK": sk},
            "UpdateExpression": full_update_expr,
            "ExpressionAttributeValues": exp_vals,
        }
        if exp_names:
            update_kwargs["ExpressionAttributeNames"] = exp_names

        self._table.update_item(**update_kwargs)
        LOGGER.info(
            "Recorded outcome '%s' for %.2fkg in session %s#%s",
            outcome,
            quantity_kg,
            service_region,
            session_date,
        )

    @with_dynamodb_retry
    def reconcile_session_metrics(
        self,
        service_region: str,
        date_str: str | None = None,
        donations_repo: DonationsRepository | None = None,
    ) -> SessionContext:
        """Reconcile session metrics against authoritative fulfilled records.

        Queries DonationsTable GSI 'region-date-status-index' via donations_repo
        to compute exact fulfilled totals and updates session accordingly.

        Args:
            service_region: Target operational region.
            date_str: Optional operational date.
            donations_repo: Optional DonationsRepository instance.

        Returns:
            Reconciled SessionContext.
        """
        session_date = self._format_date(date_str)
        pk, sk = self._build_keys(service_region, session_date)
        d_repo = donations_repo or DonationsRepository()

        summary: RunningSummary = d_repo.get_authoritative_daily_summary(
            service_region, session_date
        )

        # Ensure session exists
        self.get_or_create_session(service_region, session_date)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        response = self._table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=(
                "SET total_kg_routed = :auth_kg, "
                "total_donations_processed = :auth_count, "
                "updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":auth_kg": Decimal(str(summary.total_kg_routed)),
                ":auth_count": summary.donations_count,
                ":now": now_iso,
            },
            ReturnValues="ALL_NEW",
        )

        item = response.get("Attributes", {})
        LOGGER.info(
            "Reconciled session %s#%s: %.2fkg, %d fulfilled donations",
            service_region,
            session_date,
            summary.total_kg_routed,
            summary.donations_count,
        )
        return self._item_to_session_context(item)

    @staticmethod
    def _item_to_session_context(item: dict[str, Any]) -> SessionContext:
        """Convert raw DynamoDB item to validated SessionContext model."""
        raw_recipients = item.get("recipients_near_capacity", [])
        if isinstance(raw_recipients, (set, frozenset, list)):
            recipients_list = sorted(raw_recipients)
        else:
            recipients_list = []

        raw_volunteers = item.get("recent_volunteer_assignments", {})
        volunteers_dict = (
            {k: int(v) for k, v in raw_volunteers.items()}
            if isinstance(raw_volunteers, dict)
            else {}
        )

        return SessionContext(
            session_id=str(item["session_id"]),
            service_region=str(item["service_region"]),
            session_date=str(item["session_date"]),
            recipients_near_capacity=recipients_list,
            recent_volunteer_assignments=volunteers_dict,
            total_donations_processed=int(item.get("total_donations_processed", 0)),
            total_kg_routed=float(item.get("total_kg_routed", 0.0)),
            active_escalations_count=int(item.get("active_escalations_count", 0)),
            created_at=datetime.fromisoformat(str(item["created_at"])),
            updated_at=datetime.fromisoformat(str(item["updated_at"])),
            ttl_epoch=int(item["ttl_epoch"]),
        )
