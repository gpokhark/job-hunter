from ..models import HealthStatus, SourceHealth
from .base import AdapterError, JobAdapter


class UnsupportedAdapter(JobAdapter):
    async def fetch_summaries(self):
        raise AdapterError(self.company.unsupported_reason or "Source is unsupported")

    async def healthcheck(self):
        return SourceHealth(
            source_key=self.source_key,
            company=self.company.company,
            status=HealthStatus.UNSUPPORTED,
            message=self.company.unsupported_reason,
        )
