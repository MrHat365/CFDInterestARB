"""Rolling M1 closes; polling frequency never changes the statistical window."""
from __future__ import annotations

from collections import OrderedDict
import math
import time
import numpy as np

from .series_data import normalize_series


class MinuteStatistics:
    def __init__(self, window=500):
        self.window = max(10, int(window))
        self.points: OrderedDict[int, float] = OrderedDict()
        self.current = -1
        self.live: dict[int, float] = {}

    def seed(self, data):
        times, values = normalize_series(data.get('t', []), data.get('v', []))
        points = {int(t // 60): float(v) for t, v in zip(times, values)}
        # Preserve observations made while history was in flight, including
        # the latest minute's updated close. No filling across market closures.
        latest = max(points, default=-1)
        points.update({t: v for t, v in self.live.items() if t >= latest})
        self.points = OrderedDict(sorted(points.items())[-(self.window + 1):])
        self.current = max(self.current, int(time.time() // 60))

    def append(self, timestamp, value):
        minute = int(timestamp // 60)
        if minute < self.current:
            return
        self.current = minute
        self.points[minute] = float(value)
        self.live[minute] = float(value)
        while len(self.points) > self.window + 1:
            self.points.popitem(last=False)
        self.live = {t: v for t, v in self.live.items() if t >= minute - self.window}

    def stats(self, last, prior):
        values = [v for t, v in self.points.items() if self.current - self.window <= t < self.current][-self.window:]
        n = len(values)
        if n < 2:
            if not prior:
                return {}
            mean, variance = prior['mean'], prior['std'] ** 2
            weight = 0.
        else:
            array = np.asarray(values)
            mean, variance = float(array.mean()), float(array.var(ddof=1))
            if prior:
                # Blend moments during warmup to avoid an abrupt prior/live switch.
                weight = min(1., n / self.window)
                delta = mean - prior['mean']
                variance = ((1 - weight) * prior['std'] ** 2 + weight * variance
                            + weight * (1 - weight) * delta ** 2)
                mean = (1 - weight) * prior['mean'] + weight * mean
            else:
                if n < 10:
                    return {}
                weight = 1.
        floor = prior.get('std', 0.) * .1
        std = max(math.sqrt(max(0., variance)), floor, 1e-12)
        return {'mean': mean, 'std': std, 'z': (last - mean) / std,
                'last': last, 'n_points': n, 'prior': weight < 1.,
                'sample_period_s': 60, 'window_minutes': self.window}
