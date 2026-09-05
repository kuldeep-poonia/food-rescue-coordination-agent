"""Geographic distance computation services for donation and volunteer routing.

Provides geodesic (Haversine) calculations for local offline operations and
an extensible provider interface for Amazon Location Service integration.
"""

import math
from typing import Any, Protocol

from models import Coordinates


class DistanceCalculator(Protocol):
    """Structural interface for route and geographic distance providers."""

    def calculate_distance_km(
        self, origin: Coordinates, destination: Coordinates
    ) -> float:
        """Compute the travel or geodesic distance between two points in km.

        Args:
            origin: Source geographic coordinates.
            destination: Target geographic coordinates.

        Returns:
            Non-negative distance in kilometers.
        """
        ...


class GeodesicDistanceCalculator:
    """Computes pure spherical geodesic distance via the Haversine formula."""

    # Earth's mean spherical radius in kilometers
    EARTH_RADIUS_KM: float = 6371.0

    def calculate_distance_km(
        self, origin: Coordinates, destination: Coordinates
    ) -> float:
        """Compute Great Circle distance between two coordinate pairs.

        Args:
            origin: Starting Coordinates (lat, lon).
            destination: Destination Coordinates (lat, lon).

        Returns:
            Distance in kilometers rounded to two decimal places.
        """
        if (
            origin.latitude == destination.latitude
            and origin.longitude == destination.longitude
        ):
            return 0.0

        lat1_rad = math.radians(origin.latitude)
        lon1_rad = math.radians(origin.longitude)
        lat2_rad = math.radians(destination.latitude)
        lon2_rad = math.radians(destination.longitude)

        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad

        a = (
            math.sin(dlat / 2.0) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
        )
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        distance = self.EARTH_RADIUS_KM * c
        return round(max(0.0, distance), 2)


class AmazonLocationDistanceCalculator:
    """Calculates route travel distance via Amazon Location Service RouteCalculator."""

    def __init__(
        self,
        location_client: Any | None = None,
        calculator_name: str = "frca-route-calculator-placeholder",
        fallback_calculator: DistanceCalculator | None = None,
    ) -> None:
        """Initialize with optional boto3 client and fallback calculator.

        Args:
            location_client: Optional boto3 LocationService client.
            calculator_name: Target AWS RouteCalculator resource identifier.
            fallback_calculator: Backup calculator if AWS API is unavailable.
        """
        self._client = location_client
        self._calculator_name = calculator_name
        self._fallback = fallback_calculator or GeodesicDistanceCalculator()

    def calculate_distance_km(
        self, origin: Coordinates, destination: Coordinates
    ) -> float:
        """Calculate road network driving distance via AWS or fallback.

        Args:
            origin: Departure geographic coordinates.
            destination: Arrival geographic coordinates.

        Returns:
            Distance in kilometers.
        """
        if self._client is not None:
            try:
                response = self._client.calculate_route(
                    CalculatorName=self._calculator_name,
                    DeparturePosition=[origin.longitude, origin.latitude],
                    DestinationPosition=[destination.longitude, destination.latitude],
                    TravelMode="Car",
                )
                summary = response.get("Summary", {})
                distance_km = summary.get("Distance", 0.0)
                return round(float(distance_km), 2)
            except Exception:
                # Degrade safely to geodesic fallback when AWS service is unreachable
                return self._fallback.calculate_distance_km(origin, destination)

        return self._fallback.calculate_distance_km(origin, destination)
