"""N19: a fetcher's own account of what it tried and what failed.

Every news fetcher fails soft on purpose (one bad feed must not blank the board),
which left the caller unable to tell "fetched nothing" from "could not fetch".
Between 2026-09-12 and 09-14 the live sweep recorded NewsAPI.ai healthy through
38 straight HTTP 403s because ``fetch`` swallowed each one and returned ``[]``.
A fetcher now accepts an optional ``report`` out-param and counts every request
it makes and every one that failed; the return contract is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FetchReport:
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def attempt(self) -> None:
        self.attempts += 1

    def fail(self, error: str) -> None:
        self.errors.append(error)

    @property
    def ok(self) -> bool:
        """True unless every attempt this cycle failed. A cycle with nothing to
        try (no key, no keywords) is not a failure; a cycle where some requests
        of many failed is degraded but still delivering, and its per-request
        errors are already in the log."""
        return self.attempts == 0 or len(self.errors) < self.attempts

    def summary(self) -> str | None:
        if self.ok:
            return None
        return f"{len(self.errors)}/{self.attempts} failed: {self.errors[0]}"
