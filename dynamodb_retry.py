"""DynamoDB retry and exponential backoff policy for transient exceptions.

Handles throttling, provisioned throughput exceedance, and transient service
errors with configurable exponential backoff and jitter.
"""

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from botocore.exceptions import ClientError

from config import DEFAULT_CLIENT_TIMEOUT_SECONDS, MAX_TRANSIENT_RETRY_ATTEMPTS

F = TypeVar("F", bound=Callable[..., Any])
LOGGER: logging.Logger = logging.getLogger(__name__)

# Transient error codes that warrant automated retry with backoff
RETRIABLE_DYNAMO_ERROR_CODES: frozenset[str] = frozenset({
    "InternalServerError",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "ThrottlingException",
})

# Base backoff interval in seconds for exponential calculations
BASE_BACKOFF_SECONDS: float = 0.1


def with_dynamodb_retry(operation: F) -> F:
    """Wrap a DynamoDB callable with exponential backoff on transient throttling.

    Args:
        operation: Function performing a DynamoDB SDK call.

    Returns:
        Callable wrapped with retry logic.

    Raises:
        ClientError: If error is non-retriable or retry attempts are exhausted.
    """
    @functools.wraps(operation)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempts: int = 0
        backoff: float = BASE_BACKOFF_SECONDS

        while True:
            try:
                return operation(*args, **kwargs)
            except ClientError as exc:
                attempts += 1
                error_code: str = exc.response.get("Error", {}).get("Code", "")

                if (
                    error_code in RETRIABLE_DYNAMO_ERROR_CODES
                    and attempts <= MAX_TRANSIENT_RETRY_ATTEMPTS
                ):
                    sleep_duration: float = min(
                        backoff * (2 ** (attempts - 1)),
                        float(DEFAULT_CLIENT_TIMEOUT_SECONDS),
                    )
                    LOGGER.warning(
                        "Transient DynamoDB error %s. Retry %d/%d after %.2fs",
                        error_code,
                        attempts,
                        MAX_TRANSIENT_RETRY_ATTEMPTS,
                        sleep_duration,
                    )
                    time.sleep(sleep_duration)
                    continue

                # Re-raise when non-retriable or retries exhausted
                raise

    return wrapper  # type: ignore[return-value]
