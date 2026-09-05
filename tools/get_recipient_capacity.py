"""Tool querying real-time capacity and dietary constraints for active recipients."""

from models import Recipient
from recipients_repo import RecipientsRepository
from tools.logging_utils import get_structured_logger

LOGGER = get_structured_logger(__name__)


def get_recipient_capacity(
    service_region: str,
    recipients_repo: RecipientsRepository | None = None,
) -> list[Recipient]:
    """Retrieve capacity and constraints for active recipients in a region.

    Queries the RecipientsTable global secondary index for the service region,
    filtering for organizations with active operational status.

    Args:
        service_region: Target geographic operational region identifier.
        recipients_repo: Optional pre-configured RecipientsRepository instance.

    Returns:
        List of active Recipient models with current capacity and dietary needs.
    """
    repo = recipients_repo or RecipientsRepository()
    LOGGER.info(
        "Querying active recipient capacity for region %s",
        service_region,
        extra={"details": {"service_region": service_region}},
    )

    recipients = repo.query_active_recipients_by_region(service_region)
    # Ensure active status guarantee
    active_recipients = [r for r in recipients if r.is_active]

    LOGGER.info(
        "Retrieved %d active recipients for region %s",
        len(active_recipients),
        service_region,
        extra={
            "details": {
                "service_region": service_region,
                "count": len(active_recipients),
            }
        },
    )
    return active_recipients
