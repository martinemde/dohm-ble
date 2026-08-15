"""Tests for the re-bond escalation policy.

The bug these cover: the failure count used to live on the coordinator, and
Home Assistant builds a fresh coordinator for every setup retry, so a Dohm that
failed during setup reset the count on each attempt and never escalated.
"""

import pytest

from custom_components.dohm.health import REBOND_AFTER_FAILURES, DohmHealth


def test_a_single_failure_does_not_escalate():
    # Re-bonding is destructive -- it deletes the working bond and the device
    # only grants a new one in pairing mode -- so a blip must not trigger it.
    assert DohmHealth().note_failure() is False


def test_escalates_exactly_at_the_threshold():
    health = DohmHealth()
    results = [health.note_failure() for _ in range(REBOND_AFTER_FAILURES)]
    assert results[:-1] == [False] * (REBOND_AFTER_FAILURES - 1)
    assert results[-1] is True


def test_escalates_only_once_per_outage():
    health = DohmHealth()
    for _ in range(REBOND_AFTER_FAILURES):
        health.note_failure()
    # Repeating it would churn the proxy's NVS and keep the device connected
    # to nothing; the repair notification is the actionable part by then.
    assert [health.note_failure() for _ in range(5)] == [False] * 5


def test_failures_survive_a_new_coordinator():
    # The regression under test: the health object outlives the coordinator,
    # so failures counted during separate setup attempts still add up.
    health = DohmHealth()
    escalated = False
    for _ in range(REBOND_AFTER_FAILURES):
        # Each iteration stands in for a fresh coordinator built by a setup retry.
        escalated = health.note_failure() or escalated
    assert escalated


def test_success_clears_the_count_so_the_next_outage_escalates_again():
    health = DohmHealth()
    for _ in range(REBOND_AFTER_FAILURES):
        health.note_failure()
    assert health.note_success() is True

    results = [health.note_failure() for _ in range(REBOND_AFTER_FAILURES)]
    assert results[-1] is True


def test_success_while_healthy_is_not_a_recovery():
    # Drives whether the repair issue gets deleted; doing it on every good poll
    # would be pointless churn.
    assert DohmHealth().note_success() is False


def test_a_recovery_partway_to_the_threshold_resets_the_count():
    health = DohmHealth()
    health.note_failure()
    health.note_success()
    assert health.failures == 0
    # Not carried over: the next outage gets the full budget again.
    assert [health.note_failure() for _ in range(REBOND_AFTER_FAILURES - 1)] == [
        False
    ] * (REBOND_AFTER_FAILURES - 1)


@pytest.mark.parametrize("threshold", [REBOND_AFTER_FAILURES])
def test_threshold_is_more_than_one(threshold):
    # Guards the design decision, not the arithmetic: escalating on the first
    # failure would destroy a good bond on ordinary radio noise.
    assert threshold > 1
