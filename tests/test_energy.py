"""Tests for the power-to-energy integrator - no Home Assistant, no hardware."""

from __future__ import annotations

import math

import pytest

from .loader import load_module

energy = load_module("energy")

HOUR = 3600


def test_a_steady_kilowatt_for_an_hour_is_one_kwh() -> None:
    integrator = energy.PowerIntegrator()
    integrator.update(0, 1000)

    assert integrator.update(HOUR, 1000) == pytest.approx(1.0)


def test_the_first_reading_accrues_nothing() -> None:
    integrator = energy.PowerIntegrator()

    assert integrator.update(500, 2000) == 0.0


def test_a_reading_is_held_until_the_next_one() -> None:
    """100 W for half an hour, then 500 W: the change is not back-dated.

    The Bluetooth transport only reports a value when it changes, so the 100 W
    really did last until the 500 W arrived. Joining the points with a line
    would have counted an average of 300 W over that half hour.
    """
    integrator = energy.PowerIntegrator()
    integrator.update(0, 100)
    assert integrator.update(HOUR / 2, 500) == pytest.approx(0.05)

    assert integrator.update(HOUR, 500) == pytest.approx(0.05 + 0.25)


def test_nothing_accrues_while_there_is_no_live_reading() -> None:
    integrator = energy.PowerIntegrator()
    integrator.update(0, 1000)

    # The link drops after an hour: that hour still counts, the outage does not.
    assert integrator.update(HOUR, None) == pytest.approx(1.0)
    assert integrator.update(3 * HOUR, None) == pytest.approx(1.0)

    # It comes back; accrual restarts from the first reading, not before it.
    assert integrator.update(4 * HOUR, 1000) == pytest.approx(1.0)
    assert integrator.update(5 * HOUR, 1000) == pytest.approx(2.0)


def test_a_restored_total_is_continued() -> None:
    integrator = energy.PowerIntegrator(total_kwh=12.5)
    integrator.update(0, 60)

    assert integrator.update(HOUR, 60) == pytest.approx(12.56)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -3.0])
def test_an_unusable_restored_total_starts_again_from_zero(bad: float) -> None:
    assert energy.PowerIntegrator(total_kwh=bad).total_kwh == 0.0


def test_negative_power_counts_as_zero() -> None:
    integrator = energy.PowerIntegrator()
    integrator.update(0, -500)

    assert integrator.update(HOUR, -500) == 0.0


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_a_reading_that_is_not_a_number_is_treated_as_no_reading(bad: float) -> None:
    integrator = energy.PowerIntegrator()
    integrator.update(0, 1000)
    integrator.update(HOUR, bad)

    assert integrator.update(2 * HOUR, bad) == pytest.approx(1.0)


def test_a_clock_that_stands_still_or_steps_back_changes_nothing() -> None:
    integrator = energy.PowerIntegrator()
    integrator.update(100, 1000)

    assert integrator.update(100, 1000) == 0.0
    assert integrator.update(50, 1000) == 0.0


def test_the_total_never_goes_down() -> None:
    """total_increasing: a decrease would be read as a meter reset."""
    integrator = energy.PowerIntegrator()
    readings = [0, 250, None, 1200, -40, 80, None, None, 3000, 0, 15]
    previous = 0.0

    for step, power in enumerate(readings):
        total = integrator.update(step * 97.0, power)
        assert total >= previous
        previous = total
