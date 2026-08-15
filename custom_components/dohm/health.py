"""Failure tracking that has to outlive a single coordinator.

Home Assistant builds a *fresh* coordinator for every setup retry, so counting
consecutive failures on the coordinator instance means a Dohm that fails during
setup never reaches the re-bond threshold -- the counter resets before it can
get there. That left the one case most in need of escalation (a stale bond
found at startup) retrying silently forever, with no re-bond and no repair
notification.

Keeping the count here, owned by the config entry rather than the coordinator,
makes a failure at setup count for exactly as much as a failure while running.

Kept free of Home Assistant imports so the escalation policy is testable
without a full Home Assistant install, like the rest of the vendored engine.
"""

from __future__ import annotations

from dataclasses import dataclass

# Consecutive failures before we stop assuming a transient dropout and treat
# the link as bonded-but-dead. Counted across setup retries as well as polls,
# so the escalation runs once per outage rather than once per coordinator.
REBOND_AFTER_FAILURES = 3


@dataclass
class DohmHealth:
    """Consecutive-failure state for one configured Dohm."""

    failures: int = 0
    rebonded: bool = False

    def note_failure(self) -> bool:
        """Count a failure. True means this is the moment to re-bond.

        Returns True exactly once per outage: repeating the re-bond every
        cycle would only churn the proxy's NVS and keep the device connected
        to nothing.
        """
        self.failures += 1
        if self.failures < REBOND_AFTER_FAILURES or self.rebonded:
            return False
        self.rebonded = True
        return True

    def note_success(self) -> bool:
        """Clear the failure state. True means we had been failing."""
        if not self.failures:
            return False
        self.failures = 0
        self.rebonded = False
        return True
