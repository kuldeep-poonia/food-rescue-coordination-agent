"""Hardcore concurrency and load test for classify_donation and find_best_match."""

import concurrent.futures
import time
from datetime import datetime, timedelta, timezone

from models import Coordinates, Donation, FoodCategory, Recipient, RecipientStatus
from tools.classify_donation import classify_donation
from tools.find_best_match import find_best_match


def test_concurrent_load_two_hundred_donations() -> None:
    """Fire 200 concurrent donations through classification and matching.

    Hardcore requirement: Assert p99 latency stays within defined bound (<50ms)
    and zero requests are dropped, corrupted, or raise unhandled exceptions.
    """
    now = datetime.now(timezone.utc)

    # 1. Create a diverse pool of 20 regional recipient organizations
    categories = list(FoodCategory)
    recipients: list[Recipient] = [
        Recipient(
            recipient_id=f"rec-{i:03d}",
            organization_name=f"Community Kitchen {i}",
            contact_name=f"Coordinator {i}",
            contact_phone=f"+121255501{i:02d}",
            address=f"{100 + i} Charity Blvd, NY",
            coordinates=Coordinates(
                latitude=40.7128 + (i * 0.005),
                longitude=-74.0060 - (i * 0.005),
            ),
            capacity_kg_remaining=50.0 + (i * 10.0),
            dietary_requirements=[categories[i % len(categories)].value],
            dietary_exclusions=[],
            status=RecipientStatus.ACTIVE,
            service_region="metro-core",
        )
        for i in range(20)
    ]

    # 2. Create 200 distinct donation models
    donations: list[Donation] = [
        Donation(
            donation_id=f"don-load-{j:04d}",
            donor_id=f"donor-{j % 10}",
            donor_name=f"Donor Outlet {j % 10}",
            donor_phone="+12125550199",
            donor_address=f"{200 + j} Market St, NY",
            donor_coordinates=Coordinates(
                latitude=40.7128 + ((j % 20) * 0.003),
                longitude=-74.0060 + ((j % 20) * 0.003),
            ),
            food_category=categories[j % len(categories)],
            quantity_kg=10.0 + (j % 40),
            ready_by=now + timedelta(hours=2 + (j % 5)),
            perishability_hours=4.0 + (j % 8),
        )
        for j in range(200)
    ]

    def process_donation_pipeline(donation: Donation) -> tuple[str, float, bool]:
        t0 = time.perf_counter()
        clf = classify_donation(donation, current_time=now)
        res = find_best_match(donation, recipients, classification=clf)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Assert no corruption: returned donation_id must match input
        is_valid = (
            res.donation_id == donation.donation_id
            and (res.best_match is not None or res.rejection_reason is not None)
        )
        return donation.donation_id, elapsed_ms, is_valid

    latencies: list[float] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            executor.submit(process_donation_pipeline, don) for don in donations
        ]
        for fut in concurrent.futures.as_completed(futures):
            don_id, latency_ms, valid = fut.result()
            assert valid is True
            assert don_id.startswith("don-load-")
            latencies.append(latency_ms)

    # 3. Assert zero dropped requests
    assert len(latencies) == 200

    # 4. Latency analysis
    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]

    # Hardcore bound: p99 latency must stay strictly below 50ms for scoring
    assert p99 < 50.0, f"p99 latency {p99:.2f}ms exceeded bound of 50.0ms"
    assert p95 < 30.0, f"p95 latency {p95:.2f}ms exceeded bound of 30.0ms"
    assert p50 < 20.0, f"p50 latency {p50:.2f}ms exceeded bound of 20.0ms"
