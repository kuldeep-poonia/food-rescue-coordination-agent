"""Unit tests for get_recipient_capacity tool querying regional capacity."""

from unittest import mock

from models import Coordinates, Recipient, RecipientStatus
from recipients_repo import RecipientsRepository
from tools.get_recipient_capacity import get_recipient_capacity


def create_sample_recipient(
    recipient_id: str,
    status: RecipientStatus,
    capacity_kg: float,
    region: str = "metro-east",
) -> Recipient:
    """Helper creating a valid Recipient instance for testing."""
    return Recipient(
        recipient_id=recipient_id,
        organization_name=f"Org {recipient_id}",
        contact_name="Coordinator Contact",
        contact_phone="+12125550188",
        address="100 Charity Lane",
        coordinates=Coordinates(latitude=40.7128, longitude=-74.0060),
        capacity_kg_remaining=capacity_kg,
        status=status,
        service_region=region,
    )


def test_get_recipient_capacity_filters_active_only() -> None:
    """Verify tool returns only active recipients from the repository query."""
    r1 = create_sample_recipient("rec-001", RecipientStatus.ACTIVE, 100.0)
    r2 = create_sample_recipient("rec-002", RecipientStatus.INACTIVE, 50.0)

    mock_repo = mock.create_autospec(RecipientsRepository, instance=True)
    mock_repo.query_active_recipients_by_region.return_value = [r1, r2]

    results = get_recipient_capacity("metro-east", recipients_repo=mock_repo)
    assert len(results) == 1
    assert results[0].recipient_id == "rec-001"
    assert results[0].is_active is True
    mock_repo.query_active_recipients_by_region.assert_called_once_with("metro-east")


def test_get_recipient_capacity_handles_empty_region() -> None:
    """Verify tool handles regions with zero active organizations cleanly."""
    mock_repo = mock.create_autospec(RecipientsRepository, instance=True)
    mock_repo.query_active_recipients_by_region.return_value = []

    results = get_recipient_capacity("rural-west", recipients_repo=mock_repo)
    assert results == []
