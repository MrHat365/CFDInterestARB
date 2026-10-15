"""Finite, sorted, unique time series shared by history and Qt chart adapters."""
import math
import numpy as np

MAX_POINTS = 200_000


def peak_envelope(times, values, left, right, pixels):
    """Keep actual min/max samples per screen column, endpoints and time order.

    Inputs are finite, sorted SeriesBuffer views. Only rendering is reduced;
    statistics, crosshair lookup and strategy data keep their full resolution.
    Unlike uniform-spacing downsampling this supports history + irregular ticks.
    """
    n = len(times)
    pixels = max(1, int(pixels))
    span = right - left
    if n <= 2 * pixels + 2 or not math.isfinite(span) or span <= 0:
        return times, values
    columns = np.clip((times - left) / span * pixels, 0, pixels - 1).astype(np.int64)
    starts = np.concatenate(([0], np.flatnonzero(np.diff(columns)) + 1))
    sizes = np.diff(np.concatenate((starts, [n])))
    minimum = np.repeat(np.minimum.reduceat(values, starts), sizes)
    maximum = np.repeat(np.maximum.reduceat(values, starts), sizes)
    indices = np.arange(n)
    low = np.minimum.reduceat(np.where(values == minimum, indices, n), starts)
    high = np.minimum.reduceat(np.where(values == maximum, indices, n), starts)
    selected = np.unique(np.concatenate(([0], low, high, [n - 1])))
    return times[selected], values[selected]


def normalize_series(times, values, limit=MAX_POINTS):
    if len(times) != len(values):
        raise ValueError("时间与价格数组长度不一致")
    points = {}
    for timestamp, value in zip(times, values):
        try:
            t, v = float(timestamp), float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        # UNIX seconds, with datetime-safe bounds. Reject millisecond payloads.
        if math.isfinite(t) and 0 <= t <= 253402300799 and math.isfinite(v):
            points[t] = v  # last occurrence wins, preserving the original pair
    ordered = sorted(points)[-limit:]
    return np.asarray(ordered, dtype=float), np.asarray([points[t] for t in ordered], dtype=float)


def quotes_ready(legs, ticks, now, max_age=60.0, max_skew=1.0):
    times = []
    for leg in legs:
        tick = ticks.get(leg["symbol"])
        if tick is None:
            return False
        try:
            bid, ask = float(tick["bid"]), float(tick["ask"])
            ts = float(tick.get("time_msc", 0)) / 1000 or float(tick["time"])
            age_limit = float(tick.get("quote_max_age", max_age))
            read_at = float(tick.get("received_at", ts))
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(x) for x in (bid, ask, ts, age_limit, read_at)):
            return False
        if (age_limit <= 0 or bid <= 0 or ask < bid or not -2 <= now - ts <= age_limit
                or ('received_at' in tick and not -2 <= now - read_at <= 1.0)):
            return False
        times.append(read_at)
    return bool(times) and max(times) - min(times) <= max_skew


class SeriesBuffer:
    """Amortized O(1) ordered appends, contiguous read views, bounded allocation."""
    def __init__(self, capacity=MAX_POINTS):
        self.capacity = capacity
        self._times = np.empty(capacity * 2, dtype=float)
        self._values = np.empty(capacity * 2, dtype=float)
        self._start = self._end = 0

    @property
    def times(self):
        return self._times[self._start:self._end]

    @property
    def values(self):
        return self._values[self._start:self._end]

    def clear(self):
        self._start = self._end = 0

    def replace(self, times, values, preserve_live=False):
        times, values = normalize_series(times, values, self.capacity)
        if preserve_live and len(times):
            i = np.searchsorted(self.times, times[-1], side="right")
            times, values = normalize_series(np.concatenate((times, self.times[i:])),
                                             np.concatenate((values, self.values[i:])), self.capacity)
        self.clear()
        self._end = len(times)
        self._times[:self._end] = times
        self._values[:self._end] = values

    def append(self, timestamp, value):
        try:
            timestamp, value = float(timestamp), float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if not (math.isfinite(timestamp) and 0 <= timestamp <= 253402300799
                and math.isfinite(value)):
            return False
        if self._end > self._start:
            if timestamp < self._times[self._end - 1]:
                return False
            if timestamp == self._times[self._end - 1]:
                self._values[self._end - 1] = value
                return True
        if self._end == len(self._times):
            size = self._end - self._start
            self._times[:size] = self.times
            self._values[:size] = self.values
            self._start, self._end = 0, size
        self._times[self._end], self._values[self._end] = timestamp, value
        self._end += 1
        self._start = max(self._start, self._end - self.capacity)
        return True
