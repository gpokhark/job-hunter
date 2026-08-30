from __future__ import annotations

from .models import HealthStatus, SourceHealth


def detect_count_anomaly(
    health: SourceHealth, previous_count: int | None, threshold: int = 20
) -> SourceHealth:
    if health.status != HealthStatus.OK or previous_count is None or previous_count < threshold:
        return health
    suspicious = health.job_count == 0 or health.job_count < previous_count * 0.30
    if suspicious:
        health.status = HealthStatus.WARNING
        health.message = f"Suspicious job-count drop: {previous_count} to {health.job_count}; prior jobs retained"
    return health
