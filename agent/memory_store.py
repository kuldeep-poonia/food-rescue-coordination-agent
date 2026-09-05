"""Long-term entity memory and operational pattern repository for FRCA.

Provides persistent insight and behavioral pattern storage for donors and
recipients with automatic PII sanitization via redaction.py and a 30-day TTL.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from boto3.dynamodb.conditions import Key

from config import AppConfig, load_app_configuration
from dynamodb_retry import with_dynamodb_retry
from models import MemoryEntry
from redaction import sanitize_payload_for_logging

LOGGER: logging.Logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES: frozenset[str] = frozenset({"donor", "recipient"})


class AgentMemoryStore:
    """Manages long-term pattern memory in the Sessions & Memory table."""

    def __init__(
        self,
        dynamodb_resource: Any | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize memory store with optional injected resource and configuration.

        Args:
            dynamodb_resource: Optional pre-configured boto3 DynamoDB resource.
            config: Optional application configuration instance.
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
    def _build_pk(entity_type: str, entity_id: str) -> str:
        """Construct partition key for entity memory partition."""
        return f"MEMORY#{entity_type}#{entity_id}"

    @with_dynamodb_retry
    def record_pattern(
        self,
        entity_type: str,
        entity_id: str,
        pattern_type: str,
        raw_insights: dict[str, Any],
    ) -> MemoryEntry:
        """Sanitize insights against PII and persist entity pattern record.

        Args:
            entity_type: Category of entity ('donor' or 'recipient').
            entity_id: Unique entity identifier.
            pattern_type: Classification tag (e.g., 'dropoff_preferences').
            raw_insights: Arbitrary telemetry or pattern data to scrub and store.

        Returns:
            Validated and persisted MemoryEntry domain model.

        Raises:
            ValueError: If entity_type is invalid.
            ClientError: If DynamoDB write fails after retries.
        """
        if entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"Invalid entity_type '{entity_type}'. Must be one of: "
                f"{sorted(VALID_ENTITY_TYPES)}"
            )

        # Scrub sensitive PII keys (phone, address, names) using shared redaction
        scrubbed_insights = sanitize_payload_for_logging(raw_insights)

        memory_id = f"mem-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        ttl = int(now.timestamp()) + (self._config.memory_ttl_days * 86400)

        pk = self._build_pk(entity_type, entity_id)
        sk = f"RECORD#{pattern_type}#{now_iso}"

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "memory_id": memory_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "pattern_type": pattern_type,
            "insights": scrubbed_insights,
            "created_at": now_iso,
            "ttl_epoch": ttl,
        }

        self._table.put_item(Item=item)
        LOGGER.info(
            "Recorded pattern %s for %s %s (Memory ID: %s, TTL: %d)",
            pattern_type,
            entity_type,
            entity_id,
            memory_id,
            ttl,
        )

        return MemoryEntry(
            memory_id=memory_id,
            entity_type=entity_type,
            entity_id=entity_id,
            pattern_type=pattern_type,
            insights=scrubbed_insights,
            created_at=now,
            ttl_epoch=ttl,
        )

    @with_dynamodb_retry
    def query_entity_patterns(
        self,
        entity_type: str,
        entity_id: str,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Retrieve recent historical patterns for an entity, newest first.

        Args:
            entity_type: Category of entity ('donor' or 'recipient').
            entity_id: Unique entity identifier.
            limit: Maximum records to return.

        Returns:
            List of MemoryEntry instances sorted descending by record time.

        Raises:
            ValueError: If entity_type is invalid.
            ClientError: If DynamoDB query fails after retries.
        """
        if entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"Invalid entity_type '{entity_type}'. Must be one of: "
                f"{sorted(VALID_ENTITY_TYPES)}"
            )

        pk = self._build_pk(entity_type, entity_id)
        response = self._table.query(
            KeyConditionExpression=Key("PK").eq(pk),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = response.get("Items", [])

        entries: list[MemoryEntry] = []
        for item in items:
            entries.append(
                MemoryEntry(
                    memory_id=str(item["memory_id"]),
                    entity_type=str(item["entity_type"]),
                    entity_id=str(item["entity_id"]),
                    pattern_type=str(item["pattern_type"]),
                    insights=item.get("insights", {}),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                    ttl_epoch=int(item["ttl_epoch"]),
                )
            )

        return entries
