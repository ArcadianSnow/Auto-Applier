"""Abstract base class for all job site platform adapters."""

from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext, Page

from auto_applier.storage.models import Job, SkillGap


class JobPlatform(ABC):
    """Every job site adapter implements this interface.

    The run loop in main.py calls only these methods — it never
    imports anything platform-specific.
    """

    name: str  # Human-readable, e.g. "LinkedIn"
    source_id: str  # Short key for CSV storage, e.g. "linkedin"

    def __init__(self, context: BrowserContext, config: dict) -> None:
        self.context = context
        self.config = config
        self._page: Page | None = None

    async def get_page(self) -> Page:
        """Get or create a page for this platform."""
        if self._page is None or self._page.is_closed():
            self._page = await self.context.new_page()
        return self._page

    @abstractmethod
    async def ensure_logged_in(self) -> bool:
        """Authenticate with the platform. Return True on success."""

    @abstractmethod
    async def search_jobs(self, keyword: str, location: str) -> list[Job]:
        """Search for jobs matching keyword/location. Return Job objects."""

    @abstractmethod
    async def get_job_description(self, job: Job) -> str:
        """Fetch full description text for a job listing."""

    @abstractmethod
    async def apply_to_job(
        self, job: Job, dry_run: bool = False,
    ) -> tuple[bool, list[SkillGap]]:
        """Attempt to apply to a job.

        Returns (success, list_of_skill_gaps_found).
        """
