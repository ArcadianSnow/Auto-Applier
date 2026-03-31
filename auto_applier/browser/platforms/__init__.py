"""Platform registry -- maps source_id strings to platform adapter classes."""
from auto_applier.browser.platforms.linkedin import LinkedInPlatform
from auto_applier.browser.platforms.indeed import IndeedPlatform
from auto_applier.browser.platforms.dice import DicePlatform
from auto_applier.browser.platforms.ziprecruiter import ZipRecruiterPlatform

PLATFORM_REGISTRY: dict[str, type] = {
    "linkedin": LinkedInPlatform,
    "indeed": IndeedPlatform,
    "dice": DicePlatform,
    "ziprecruiter": ZipRecruiterPlatform,
}
