from __future__ import annotations


class AgentException(Exception):
    def __init__(self, data):
        self.data = data


class BenchNotExistsException(Exception):
    def __init__(self, bench):
        self.bench = bench
        self.message = f"Bench {bench} does not exist"

        super().__init__(self.message)


class SiteNotExistsException(Exception):
    def __init__(self, site, bench):
        self.site = site
        self.bench = bench
        self.message = f"Site {site} does not exist on bench {bench}"

        super().__init__(self.message)


class InvalidSiteConfigException(AgentException):
    def __init__(self, data: dict, site=None):
        self.site = site
        super().__init__(data)


class RegistryDownException(Exception):
    def __init__(self, data):
        self.data = data


class LowMemoryException(Exception):
    """Raised when a build refuses to start because the host is low on RAM.

    Press's agent-job poller marks the job as Failure on this exception;
    Press auto-retries Failed builds on its normal schedule, so the build
    will run as soon as memory frees. Prevents OOM cascades when many
    deploys land at the same second.
    """

    def __init__(self, available_mb: int, required_mb: int):
        self.available_mb = available_mb
        self.required_mb = required_mb
        self.message = (
            f"Refusing to start build: only {available_mb} MB available, "
            f"need {required_mb} MB. Will retry on next agent poll."
        )
        super().__init__(self.message)
