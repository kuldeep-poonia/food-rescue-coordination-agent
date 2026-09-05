"""Hardcore test suite for cross-table claim transactions and unwind compensation."""

from unittest import mock

import pytest
from botocore.exceptions import ClientError

from donations_repo import (
    DonationClaimConflictError,
    DonationsRepository,
    DonationStateConflictError,
)
from idempotency import build_idempotency_key
from models import (
    InfrastructureConsistencyError,
)
from recipients_repo import (
    InsufficientCapacityError,
)


def test_claim_and_deduct_transaction_atomicity() -> None:
    """Verify claim_and_deduct_recipient issues atomic TransactWriteItems with token."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client
    repo = DonationsRepository(dynamodb_resource=mock_resource)

    # Success execution
    mock_client.transact_write_items.return_value = {}
    success = repo.claim_and_deduct_recipient(
        donation_id="don-trans-01",
        recipient_id="rec-target-01",
        quantity_kg=40.0,
    )
    assert success is True
    assert mock_client.transact_write_items.call_count == 1
    call_kwargs = mock_client.transact_write_items.call_args[1]

    # Verify ClientRequestToken is deterministic
    expected_token = build_idempotency_key("don-trans-01", "claim_and_deduct")
    assert call_kwargs["ClientRequestToken"] == expected_token

    # Verify two TransactItems (donations_table at index 0, recipients_table at index 1)
    items = call_kwargs["TransactItems"]
    assert len(items) == 2
    assert "donations" in items[0]["Update"]["TableName"]
    assert items[0]["Update"]["Key"]["donation_id"]["S"] == "don-trans-01"
    assert "recipients" in items[1]["Update"]["TableName"]
    assert items[1]["Update"]["Key"]["recipient_id"]["S"] == "rec-target-01"


def test_claim_and_deduct_cancellation_reasons_priority() -> None:
    """Verify exact CancellationReasons index parsing and priority resolution."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client
    repo = DonationsRepository(dynamodb_resource=mock_resource)

    # 1. Index 0 (donation) failed, Index 1 ok -> DonationClaimConflictError
    mock_client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )
    with pytest.raises(DonationClaimConflictError, match="already claimed"):
        repo.claim_and_deduct_recipient("don-p-01", "rec-01", 30.0)

    # 2. Index 0 ok, Index 1 (recipient) failed -> InsufficientCapacityError
    mock_client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )
    with pytest.raises(InsufficientCapacityError, match="lacks capacity"):
        repo.claim_and_deduct_recipient("don-p-01", "rec-01", 30.0)

    # 3. Both Index 0 and Index 1 failed ->
    # Priority decision: DonationClaimConflictError
    mock_client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "ConditionalCheckFailed"},
            ],
        },
        "TransactWriteItems",
    )
    with pytest.raises(DonationClaimConflictError, match="already claimed"):
        repo.claim_and_deduct_recipient("don-p-01", "rec-01", 30.0)


def test_unclaim_and_restore_atomic_unwind() -> None:
    """Verify unclaim_and_restore_recipient issues atomic compensation transaction."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client
    repo = DonationsRepository(dynamodb_resource=mock_resource)

    mock_client.transact_write_items.return_value = {}
    success = repo.unclaim_and_restore_recipient(
        donation_id="don-unwind-01",
        recipient_id="rec-unwind-01",
        quantity_kg=25.0,
    )
    assert success is True
    call_kwargs = mock_client.transact_write_items.call_args[1]

    expected_token = build_idempotency_key("don-unwind-01", "unclaim_and_restore")
    assert call_kwargs["ClientRequestToken"] == expected_token

    items = call_kwargs["TransactItems"]
    assert len(items) == 2
    assert "REMOVE matched_recipient_id" in items[0]["Update"]["UpdateExpression"]
    assert "+ :qty" in items[1]["Update"]["UpdateExpression"]


def test_unwind_failure_raises_infrastructure_consistency_error() -> None:
    """Verify double-failure during unwind raises InfrastructureConsistencyError."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client
    repo = DonationsRepository(dynamodb_resource=mock_resource)

    # Simulate network crash during compensation transaction
    mock_client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError"}},
        "TransactWriteItems",
    )
    with pytest.raises(
        InfrastructureConsistencyError,
        match="Atomic unclaim and restore failed",
    ):
        repo.unclaim_and_restore_recipient("don-err-01", "rec-01", 20.0)


def test_unwind_state_conflict_raises_donation_state_conflict_error() -> None:
    """Verify condition check failure during unclaim raises state conflict."""
    mock_client = mock.MagicMock()
    mock_resource = mock.MagicMock()
    mock_resource.meta.client = mock_client
    repo = DonationsRepository(dynamodb_resource=mock_resource)

    mock_client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )
    with pytest.raises(DonationStateConflictError, match="not claimed by"):
        repo.unclaim_and_restore_recipient("don-err-02", "rec-wrong", 20.0)
