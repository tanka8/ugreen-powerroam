"""Turn a power reading into cumulative energy.

The PowerRoam reports watts and no energy counter, on either transport, so the
Energy dashboard's kWh have to be integrated here. Kept free of Home Assistant
imports so it is tested directly (tests/test_energy.py).
"""

from __future__ import annotations

import math

JOULES_PER_KWH = 3_600_000  # a watt-second is a joule; a kWh is 3.6 million of them


class PowerIntegrator:
    """Accumulate kWh from watts, holding each reading until the next one.

    Holding rather than joining the points with a straight line is deliberate.
    The Bluetooth transport only reports a value when it changes, so between two
    samples the power really was the earlier one, however long that was; a
    straight line would invent energy that was never drawn. The cloud pushes
    every ~15 seconds, where holding is as good as any other rule for a load
    that switches rather than ramps.

    Time spent without a live reading is not guessed at: pass ``None`` and
    nothing accrues until a reading comes back. The total never goes down, which
    is what ``total_increasing`` promises.
    """

    def __init__(self, total_kwh: float = 0.0) -> None:
        # A bad restored value (NaN, negative, infinite) starts over from zero.
        usable = math.isfinite(total_kwh) and total_kwh > 0
        self.total_kwh = total_kwh if usable else 0.0
        self._power_w: float | None = None
        self._since: float | None = None

    def update(self, now: float, power_w: float | None) -> float:
        """Count the time since the last call, then hold ``power_w`` from ``now``.

        ``now`` is any steadily increasing clock, in seconds. ``power_w`` of
        ``None`` (or a value that is not a finite number) means there is no live
        reading: the interval that has just ended is still counted at the last
        reading, up to ``now``, and nothing is counted after it. Negative
        readings count as zero. Returns the running total in kWh.
        """
        if self._power_w is not None and self._since is not None and now > self._since:
            self.total_kwh += self._power_w * (now - self._since) / JOULES_PER_KWH

        if power_w is None or not math.isfinite(power_w):
            self._power_w = None
            self._since = None
        else:
            self._power_w = max(power_w, 0.0)
            self._since = now
        return self.total_kwh
