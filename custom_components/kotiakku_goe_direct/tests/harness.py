"""Shared helpers for the kotiakku_goe_direct script tests (no Home Assistant)."""

from __future__ import annotations

import datetime
import importlib.util
import traceback
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLOT = 900


def load_mod(name, suffix=""):
    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"kotiakku_goe_direct_{name}{suffix}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fail(Exception):
    pass


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise Fail("%s: got %r expected %r" % (msg, actual, expected))


def assert_true(cond, msg):
    if not cond:
        raise Fail(msg)


def assert_gt(actual, expected, msg):
    if not actual > expected:
        raise Fail("%s: got %r not > %r" % (msg, actual, expected))


class Clock:
    def __init__(self, now, tz=timezone.utc):
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)
        else:
            now = now.astimezone(tz)
        self._now = now
        self._tz = tz

    def now(self):
        return self._now

    def advance(self, **delta):
        self._now = self._now + datetime.timedelta(**delta)
        return self._now

    def set(self, now):
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        else:
            now = now.astimezone(self._tz)
        self._now = now

    def as_timestamp(self, value):
        if value is None:
            raise ValueError("none")
        if isinstance(value, datetime.datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=self._tz)
            return dt.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        parsed = self.parse_datetime(str(value))
        if parsed is None:
            raise ValueError(value)
        return parsed.timestamp()

    def utc_from_timestamp(self, ts):
        return datetime.datetime.fromtimestamp(float(ts), tz=timezone.utc)

    def start_of_local_day(self, dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        local = dt.astimezone(self._tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def parse_datetime(self, value):
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def slots_from(start_ts, prices, slot=900):
    out = []
    t = start_ts
    for price in prices:
        out.append({"start": iso(t), "end": iso(t + slot), "value": price})
        t += slot
    return out


def window_starts(result):
    return tuple(w["start"] for w in (result or {}).get("raw_windows") or [])


def window_ends(result):
    return tuple(w["end"] for w in (result or {}).get("raw_windows") or [])


def plan_ranks(planner, clock, attrs, results, **plan_kw):
    out = dict(results)
    for rank in planner.RANKS:
        prev = planner.prev_from_result(clock, out.get(rank))
        out[rank] = planner.plan(clock, attrs, rank=rank, prev=prev, **plan_kw)
    return out


def case_runner():
    cases = []

    def case(name, fn):
        cases.append((name, fn))

    def run():
        run_cases(cases)

    return case, run


def run_cases(cases):
    failed = 0
    passed = 0
    for name, fn in cases:
        try:
            fn()
            passed += 1
            print("ok  %s" % name)
        except Exception as exc:
            failed += 1
            print("FAIL %s: %s" % (name, exc))
            if not isinstance(exc, Fail):
                traceback.print_exc()
    print("%s passed, %s failed" % (passed, failed))
    if failed:
        raise SystemExit(1)
