"""Bounded FIFO with replaceable snapshots and explicit critical backpressure.

Replaceable snapshots coalesce before saturation. Critical traffic
waits for capacity; it is never silently discarded. No Qt code belongs here.
"""
import queue
import time
from collections import OrderedDict

from .constants import EVENT_ACCOUNT, EVENT_AUTOMATION_STATE, EVENT_ORDER, EVENT_POSITION, EVENT_TICK

_REPLACEABLE = {EVENT_ACCOUNT, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_AUTOMATION_STATE}


class EventQueue(queue.Queue):
    def __init__(self, maxsize=5000):
        super().__init__(maxsize)
        self.dropped = 0

    def _init(self, maxsize):
        self.queue = OrderedDict()
        self._snapshots = OrderedDict()
        self._sequence = 0

    def _put(self, item):
        self._sequence += 1
        key = self.key(item)
        self.queue[self._sequence] = (key, item)
        if key is not None:
            self._snapshots[key] = self._sequence

    def _get(self):
        _, (key, item) = self.queue.popitem(last=False)
        if key is not None:
            del self._snapshots[key]
        return item

    def _discard_snapshot(self, key):
        del self.queue[self._snapshots.pop(key)]
        self.unfinished_tasks -= 1
        self.dropped += 1

    @staticmethod
    def key(item):
        if isinstance(item, dict):
            kind, data = item.get("type"), item
        else:
            kind, data = item.type, item.data or {}
        if kind not in _REPLACEABLE:
            return None
        if kind == EVENT_AUTOMATION_STATE and (data.get('payload') or {}).get('request_id'):
            return None  # Command acknowledgements are not replaceable telemetry.
        return kind, data.get("session"), data.get("generation")

    def put(self, item, block=True, timeout=None):
        key = self.key(item)
        with self.not_full:
            deadline = None if timeout is None else time.monotonic() + timeout
            # Coalesce before saturation; newer data follows intervening acks.
            if key is not None and key in self._snapshots:
                self._discard_snapshot(key)
            while self.maxsize > 0 and self._qsize() >= self.maxsize:
                # Prefer replacement of this account's same snapshot; preserve
                # FIFO ordering by appending the newest snapshot at the tail.
                # A blocked producer releases the mutex: a competing producer
                # may have inserted this key before we wake. Recheck uniqueness.
                victim = key if key in self._snapshots else next(iter(self._snapshots), None)
                if victim is not None:
                    self._discard_snapshot(victim)
                    break
                if key is not None:
                    self.dropped += 1
                    return
                if not block:
                    raise queue.Full
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Full
                self.not_full.wait(remaining)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
