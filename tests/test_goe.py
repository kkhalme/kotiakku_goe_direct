import asyncio
import json
from datetime import datetime, timedelta, timezone

from core.model import OFF, Command
from goe import GoeMqtt, parse_int, parse_nrg

T0 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
ON = Command(True, 1, 50, 21)


class Fake:
    def __init__(self):
        self.published = []
        self.events = []
        self.goe = GoeMqtt(["111111"], self.publish, lambda *e: self.events.append(e))

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def send(self, cmd, dt=0):
        self.published.clear()
        asyncio.run(self.goe.send("111111", cmd, T0 + timedelta(seconds=dt)))
        return [(t.split("/")[2], p) for t, p in self.published]

    def live(self, **values):
        for key, value in values.items():
            self.goe.handle(f"go-eCharger/111111/{key}", str(value))


def test_parse_nrg_and_int():
    nrg = [230, 231, 229, 0, 7, 7, 7, 0, 1600, 1600, 1600, 4830, 99, 99, 99, 99]
    assert parse_nrg(json.dumps(nrg)) == 4830
    assert parse_nrg(json.dumps({"nrg": nrg})) == 4830
    assert parse_nrg(",".join(str(v) for v in nrg)) == 4830
    assert parse_nrg([0] * 11 + [-350]) == 350
    assert parse_nrg("[1, 2]") is None and parse_nrg("garbage") is None
    assert parse_int(b"4") == 4 and parse_int("2.0") == 2 and parse_int("unknown") is None


def test_status_messages_update_live_and_notify():
    fake = Fake()
    fake.live(car=3, frc=1)
    fake.goe.handle("go-eCharger/222222/car", "4")
    fake.goe.handle("go-eController/111111/car", "4")
    fake.goe.handle("go-eCharger/111111/ama", "32")
    assert fake.goe.live["111111"] == {"car": 3, "frc": 1}
    fake.live(car=3)
    assert fake.events == [("111111", "car", None, 3), ("111111", "frc", None, 1)]


def test_on_and_off_order():
    fake = Fake()
    assert fake.send(ON) == [("fup", "false"), ("psm", "1"), ("lot", "50"), ("amp", "21"), ("frc", "2")]
    assert fake.send(OFF) == [("frc", "1"), ("fup", "false")]


def test_skip_when_live_matches():
    fake = Fake()
    fake.live(frc=2, psm=1, lot=50, amp=21)
    assert fake.send(ON) == []
    fake.live(frc=1)
    assert fake.send(OFF) == []


def test_unconfirmed_is_not_republished_every_cycle():
    fake = Fake()
    assert fake.send(ON)
    fake.live(frc=2)
    assert fake.send(ON, dt=2) == []
    assert fake.send(ON, dt=600) == []
    assert fake.send(ON, dt=900)


def test_contradiction_retries_after_30s():
    fake = Fake()
    fake.send(ON)
    fake.live(frc=1, psm=1, lot=50, amp=21)
    assert fake.send(ON, dt=10) == []
    assert fake.goe.next_retry_at() == T0 + timedelta(seconds=30)
    assert fake.send(ON, dt=30)


def test_changed_command_publishes_immediately():
    fake = Fake()
    fake.send(ON)
    assert fake.send(Command(True, 1, 50, 22), dt=1)


def test_lot_off_the_fuse_cap_is_rewritten():
    fake = Fake()
    fake.live(frc=2, psm=1, lot=19, amp=21)
    assert ("lot", "50") in fake.send(ON)


def test_neutral_after_unplug_is_forced_off():
    fake = Fake()
    fake.live(frc=0)
    assert fake.send(OFF) == [("frc", "1"), ("fup", "false")]
