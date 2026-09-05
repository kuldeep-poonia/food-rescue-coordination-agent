"""Unit and boundary tests for AgentMemoryStore PII scrubbing and retention."""

import threading
from datetime import datetime, timezone
from typing import Any

import pytest

from agent.memory_store import AgentMemoryStore
from models import MemoryEntry


class MockMemoryDynamoDBTable:
    """Thread-safe in-memory table emulating DynamoDB PK/SK query and put."""

    def __init__(self, name: str) -> None:
        self.name: str = name
        self._items: dict[str, dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def put_item(self, Item: dict[str, Any]) -> None:
        with self._lock:
            pk = str(Item["PK"])
            sk = str(Item["SK"])
            self._items[f"{pk}#{sk}"] = Item.copy()

    def query(
        self,
        KeyConditionExpression: Any = None,
        ScanIndexForward: bool = True,
        Limit: int = 10,
    ) -> dict[str, Any]:
        del KeyConditionExpression
        with self._lock:
            items = list(self._items.values())
            # Sort by SK
            items.sort(key=lambda x: str(x.get("SK", "")), reverse=not ScanIndexForward)
            return {"Items": [it.copy() for it in items[:Limit]]}


class MockMemoryDynamoDBResource:
    """Mock DynamoDB client returning isolated mock tables."""

    def __init__(self) -> None:
        self.tables: dict[str, MockMemoryDynamoDBTable] = {}

    def Table(self, name: str) -> MockMemoryDynamoDBTable:
        if name not in self.tables:
            self.tables[name] = MockMemoryDynamoDBTable(name)
        return self.tables[name]


@pytest.fixture
def mock_memory_dynamo() -> MockMemoryDynamoDBResource:
    """Provide isolated mock DynamoDB resource."""
    return MockMemoryDynamoDBResource()


def test_record_pattern_scrubs_pii(
    mock_memory_dynamo: MockMemoryDynamoDBResource,
) -> None:
    """Verify raw PII fields are sanitized before being written to DynamoDB."""
    store = AgentMemoryStore(dynamodb_resource=mock_memory_dynamo)

    raw_insights = {
        "preferred_window": "morning",
        "donor_phone": "+12125550199",
        "donor_address": "500 Broadway, New York, NY",
        "donor_name": "Downtown Bakery",
        "contact_phone": "+14155551234",
        "instructions": "Call upon arrival",
    }

    entry = store.record_pattern(
        entity_type="donor",
        entity_id="donor-100",
        pattern_type="dropoff_preference",
        raw_insights=raw_insights,
    )
    assert isinstance(entry, MemoryEntry)

    # Check that entry returned has masked PII
    assert entry.insights["donor_phone"] == "***-***-0199"
    assert entry.insights["donor_address"] == "*** Broadway, New York, NY"
    assert entry.insights["donor_name"] == "D***"
    assert entry.insights["contact_phone"] == "***-***-1234"
    assert entry.insights["preferred_window"] == "morning"

    # Check underlying table item directly to ensure no raw PII in database
    table = mock_memory_dynamo.Table(store._config.sessions_memory_table_name)
    stored_item = next(iter(table._items.values()))
    stored_insights = stored_item["insights"]
    assert stored_insights["donor_phone"] == "***-***-0199"
    assert stored_insights["donor_address"] == "*** Broadway, New York, NY"
    assert stored_insights["donor_name"] == "D***"
    assert "+12125550199" not in str(stored_item)
    assert "500 Broadway" not in str(stored_item)


def test_record_pattern_30_day_ttl(
    mock_memory_dynamo: MockMemoryDynamoDBResource,
) -> None:
    """Verify memory entry is configured with a strict 30-day TTL epoch."""
    store = AgentMemoryStore(dynamodb_resource=mock_memory_dynamo)

    entry = store.record_pattern(
        entity_type="recipient",
        entity_id="rec-200",
        pattern_type="intake_capacity_pattern",
        raw_insights={"peak_intake_hour": 14},
    )

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    expected_ttl_min = now_epoch + (30 * 86400) - 100
    expected_ttl_max = now_epoch + (30 * 86400) + 100

    assert entry.ttl_epoch >= expected_ttl_min
    assert entry.ttl_epoch <= expected_ttl_max


def test_query_entity_patterns_chronological_ordering(
    mock_memory_dynamo: MockMemoryDynamoDBResource,
) -> None:
    """Verify query returns patterns sorted descending by creation time."""
    store = AgentMemoryStore(dynamodb_resource=mock_memory_dynamo)

    e1 = store.record_pattern(
        entity_type="recipient",
        entity_id="rec-300",
        pattern_type="pattern_one",
        raw_insights={"sequence": 1},
    )
    e2 = store.record_pattern(
        entity_type="recipient",
        entity_id="rec-300",
        pattern_type="pattern_two",
        raw_insights={"sequence": 2},
    )

    results = store.query_entity_patterns("recipient", "rec-300", limit=5)

    assert len(results) == 2
    assert results[0].memory_id == e2.memory_id
    assert results[1].memory_id == e1.memory_id


def test_invalid_entity_type_rejected(
    mock_memory_dynamo: MockMemoryDynamoDBResource,
) -> None:
    """Verify entity types other than donor or recipient are strictly rejected."""
    store = AgentMemoryStore(dynamodb_resource=mock_memory_dynamo)

    with pytest.raises(ValueError, match="Invalid entity_type"):
        store.record_pattern(
            entity_type="volunteer",
            entity_id="vol-01",
            pattern_type="route_preference",
            raw_insights={},
        )

    with pytest.raises(ValueError, match="Invalid entity_type"):
        store.query_entity_patterns("coordinator", "coord-01")
