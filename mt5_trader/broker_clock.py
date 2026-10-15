"""Explicit quote/bar clock contract; never infer a timezone from a stale tick.

MT5 documents UTC epochs. UTC is the default. A verified nonstandard broker
can use a fixed UTC offset or IANA timezone, with date-specific DST conversion.
Ambiguous/nonexistent local wall times fail closed rather than guessing a fold.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BrokerClock:
    def __init__(self, name='UTC'):
        self.name = str(name or 'UTC').strip()
        self.zone: dt.tzinfo
        match = re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})', self.name)
        if self.name == 'UTC':
            self.zone = dt.timezone.utc
        elif match:
            hours, minutes = int(match[2]), int(match[3])
            offset = hours * 60 + minutes
            if minutes >= 60 or offset > 14 * 60:
                raise ValueError('报价时区偏移必须在±14小时内')
            self.zone = dt.timezone(dt.timedelta(minutes=offset * (1 if match[1] == '+' else -1)))
        else:
            try:
                self.zone = ZoneInfo(self.name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError('报价时区无效；使用UTC、UTC+03:00或已核实的IANA时区') from exc

    def to_utc(self, timestamp):
        value = float(timestamp)
        if not math.isfinite(value) or not 0 <= value <= 253402300799:
            raise ValueError('无效报价时间')
        if self.name == 'UTC':
            return value
        wall = dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None)
        candidates = set()
        for fold in (0, 1):
            aware = wall.replace(tzinfo=self.zone, fold=fold)
            utc = aware.timestamp()
            if dt.datetime.fromtimestamp(utc, self.zone).replace(tzinfo=None) == wall:
                candidates.add(utc)
        if len(candidates) != 1:
            raise ValueError('报价时间处于夏令时重复/缺失区间，无法唯一换算UTC')
        result = candidates.pop()
        if not 0 <= result <= 253402300799:
            raise ValueError("换算后的UTC超出有效范围")
        return result

    def tick(self, raw):
        data = dict(raw)
        stamp = float(data.get('time_msc') or float(data['time']) * 1000) / 1000
        utc = self.to_utc(stamp)
        data.update(time=int(utc), time_msc=int(round(utc * 1000)),
                    received_at=time.time(), read_monotonic=time.monotonic())
        return data

    def metadata(self):
        # clock_ready describes the configured basis, not market freshness.
        offset = dt.datetime.now(self.zone).utcoffset()
        return {'broker_timezone': self.name, 'server_utc_offset': offset.total_seconds() if offset is not None else 0.,
                'clock_basis': 'explicit', 'clock_ready': True}
