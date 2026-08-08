#!/usr/bin/env python3
"""Fresh isolated validation for the Host-counter listen-decoder recovery design.

No production module is imported.  All files created by this program remain in
scratch/validation_017/out.  The timeout is shortened only for test execution;
the original 3 x 2-second contract is checked statically and the arming math
uses its real 6.0-second fuse point.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time


REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
ARM_SECONDS = 10.1
STABLE_SECONDS = 900.0


class StatusError(RuntimeError):
    pass


class AtomicStatus:
    """Reference persistence protocol: sidecar flock and same-directory rename."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "listen-watchdog.json"
        self.lock_path = directory / "listen-watchdog.lock"

    @staticmethod
    def default() -> dict:
        return {"trip_count": 0, "last_trip": None, "locked": False}

    def _validate(self, data: object) -> dict:
        if not isinstance(data, dict):
            raise StatusError("status root is not an object")
        if set(data) != {"trip_count", "last_trip", "locked"}:
            raise StatusError("status schema differs")
        if not isinstance(data["trip_count"], int) or data["trip_count"] < 0:
            raise StatusError("trip_count invalid")
        if data["last_trip"] is not None and not isinstance(data["last_trip"], str):
            raise StatusError("last_trip invalid")
        if not isinstance(data["locked"], bool):
            raise StatusError("locked invalid")
        return dict(data)

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return self.default()
        try:
            with self.path.open("r", encoding="utf-8") as status_file:
                return self._validate(json.load(status_file))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StatusError(f"cannot read status: {exc}") from exc

    def _write_unlocked(self, state: dict) -> None:
        payload = json.dumps(self._validate(state), sort_keys=True, separators=(",", ":")).encode("utf-8")
        tmp_path = self.directory / f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self.path)
            dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise StatusError(f"cannot atomically write status: {exc}") from exc
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    def read(self) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def update(self, update_fn) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                after = update_fn(self._read_unlocked())
                self._write_unlocked(after)
                return after
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def increment_fuse(self, stamp: str) -> dict:
        def update(state: dict) -> dict:
            state["trip_count"] += 1
            state["last_trip"] = stamp
            if state["trip_count"] >= 3:
                state["locked"] = True
            return state
        return self.update(update)

    def reset(self) -> dict:
        return self.update(lambda _state: self.default())


def _increment_worker(directory: str, amount: int) -> None:
    store = AtomicStatus(Path(directory))
    for sequence in range(amount):
        store.increment_fuse(f"pid-{os.getpid()}-{sequence}")


class ManualClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MockRetainedBroker:
    def __init__(self):
        self.retained: dict[str, str] = {}
        self.events: list[tuple[str, str | None]] = []

    def publish(self, topic: str, payload: str | None, *, retain: bool = True) -> None:
        self.events.append((topic, payload))
        if retain:
            if payload is None:
                self.retained.pop(topic, None)
            else:
                self.retained[topic] = payload


class MockHA:
    def __init__(self, broker: MockRetainedBroker):
        self.broker = broker
        self.discovery_topic = "homeassistant/sensor/jkbms_1/voltage/config"
        self.status_topic = "gateway/jkbms/1/status"
        self.cleanup_calls = 0
        self.availability: bool | None = None

    def ensure_discovery(self) -> None:
        self.broker.publish(self.discovery_topic, "retained-discovery")

    def set_availability(self, online: bool) -> None:
        self.availability = online
        self.broker.publish(self.status_topic, "online" if online else "offline")

    def cleanup_discovery(self) -> None:
        self.cleanup_calls += 1
        self.broker.publish(self.discovery_topic, None)


class FakeDriver:
    def __init__(self):
        self.reads = 0

    async def read_stream(self, **_kwargs) -> bytes:
        self.reads += 1
        return b"fake-jkbms-frame"


class BoundedHangingJKBMS:
    adapter_name = "jkbms"

    def feed(self, _chunk: bytes) -> dict:
        time.sleep(0.05)
        return {"voltage": 52.1}


class ValidJKBMS:
    adapter_name = "jkbms"

    def feed(self, _chunk: bytes) -> dict:
        return {"voltage": 52.1}


class HostCounterDispatcherModel:
    """Corrected proposal model. os._exit is represented by exit_code only."""

    def __init__(self, driver, adapter, ha: MockHA, store: AtomicStatus, clock: ManualClock,
                 *, process_age: float, decode_timeout: float = 0.005):
        self.driver = driver
        self.adapter = adapter
        self.ha = ha
        self.store = store
        self.clock = clock
        self.process_age = process_age
        self.decode_timeout = decode_timeout
        self.timeout_streak = 0
        self.executor_submissions = 0
        self.guard_hits = 0
        self.exit_code: int | None = None
        self.exit_wait_seconds: float | None = None
        self.safe_error: str | None = None
        self.valid_jkbms_since: float | None = None
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="validation017")
        try:
            initial = store.read()  # Read only: startup never increments.
            self.decoding_disabled = bool(initial["locked"] or initial["trip_count"] >= 3)
        except StatusError as exc:
            self.decoding_disabled = True
            self.safe_error = f"status_load_failed:{exc}"
        self.ha.ensure_discovery()
        self.ha.set_availability(False)  # Required initial retained offline.

    async def process_once(self) -> None:
        _chunk = await self.driver.read_stream(chunk_size=1024, timeout=5.0)
        if self.decoding_disabled:  # This is intentionally before executor submission.
            self.guard_hits += 1
            return
        self.executor_submissions += 1
        loop = asyncio.get_running_loop()
        try:
            decoded = await asyncio.wait_for(
                loop.run_in_executor(self.executor, self.adapter.feed, _chunk),
                timeout=self.decode_timeout,
            )
        except asyncio.TimeoutError:
            self.timeout_streak += 1
            self.valid_jkbms_since = None
            if self.timeout_streak < 3:
                return
            self.timeout_streak = 0
            self.ha.set_availability(False)
            try:
                state = self.store.increment_fuse(f"t={self.clock.now:.1f}")
            except StatusError as exc:
                self.decoding_disabled = True
                self.safe_error = f"status_write_failed:{exc}"
                return
            if state["trip_count"] < 3:
                self.decoding_disabled = True  # guard while waiting for os._exit(75)
                self.exit_code = 75
                self.exit_wait_seconds = max(0.0, ARM_SECONDS - self.process_age)
            else:
                self.decoding_disabled = True
            return

        self.timeout_streak = 0
        if decoded and self.adapter.adapter_name == "jkbms" and self.valid_jkbms_since is None:
            self.valid_jkbms_since = self.clock.now
            self.ha.set_availability(True)

    def tick(self) -> bool:
        if self.decoding_disabled or self.valid_jkbms_since is None:
            return False
        if self.clock.now - self.valid_jkbms_since < STABLE_SECONDS:
            return False
        try:
            self.store.reset()
        except StatusError as exc:
            self.decoding_disabled = True
            self.safe_error = f"status_reset_failed:{exc}"
            return False
        self.valid_jkbms_since = None
        return True

    def close(self) -> None:
        # Fakes are bounded. This does not claim production can kill a stuck thread.
        self.executor.shutdown(wait=True, cancel_futures=True)


def static_baseline() -> dict:
    listen = (REPO / "src/listen_master.py").read_text(encoding="utf-8")
    main = (REPO / "src/main.py").read_text(encoding="utf-8")
    compose = (REPO / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    config = (REPO / "profile/config.yaml").read_text(encoding="utf-8")
    register = listen[listen.index("    def register_device"):listen.index("    def unregister_device")]
    assert "self._decode_timeout_threshold = 3" in listen
    assert "timeout=2.0" in listen and "loop.run_in_executor" in listen
    assert "app_state.gateway.unregister_device(uid)" in listen
    assert "_decoding_disabled" not in listen
    assert "set_availability(False)" not in register
    assert "self.start_time = time.monotonic()" in main
    assert "restart: unless-stopped" in compose
    assert "./profile:/app/profile:rw" in compose
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "mode: listen" not in config
    return {
        "assertions": 10,
        "current_fuse": "3 x 2.0s",
        "current_source_has_host_counter": False,
        "current_source_has_executor_guard": False,
        "current_source_preserves_discovery_on_fuse": False,
        "restart_policy": "unless-stopped",
        "profile_bind_mount_available": True,
        "current_config_listen_devices": 0,
    }


async def test_trip_ladder(directory: Path) -> dict:
    store = AtomicStatus(directory)
    broker = MockRetainedBroker()
    ha = MockHA(broker)
    discovery_snapshot = None
    outcomes = []
    measured = []
    for expected_count in (1, 2, 3):
        clock = ManualClock(100.0 + expected_count)
        model = HostCounterDispatcherModel(
            FakeDriver(), BoundedHangingJKBMS(), ha, store, clock, process_age=6.0
        )
        try:
            ha.set_availability(True)
            if discovery_snapshot is None:
                discovery_snapshot = dict(broker.retained)
            started = time.monotonic()
            for _ in range(3):
                await model.process_once()
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            measured.append(elapsed_ms)
            status = store.read()
            assert status["trip_count"] == expected_count
            assert broker.retained[ha.discovery_topic] == "retained-discovery"
            assert broker.retained[ha.status_topic] == "offline"
            assert ha.cleanup_calls == 0
            if expected_count < 3:
                assert model.exit_code == 75
                assert math.isclose(model.exit_wait_seconds, 4.1, abs_tol=0.000001)
                outcomes.append(f"trip{expected_count}=exit75")
            else:
                assert model.exit_code is None and model.decoding_disabled
                before = model.executor_submissions
                for _ in range(9):
                    await model.process_once()
                assert model.executor_submissions == before == 3
                assert model.guard_hits == 9
                outcomes.append("trip3=locked")
        finally:
            model.close()

    assert broker.retained[ha.discovery_topic] == discovery_snapshot[ha.discovery_topic]

    restart = HostCounterDispatcherModel(
        FakeDriver(), BoundedHangingJKBMS(), ha, store, ManualClock(200.0), process_age=20.0
    )
    try:
        assert restart.decoding_disabled
        await restart.process_once()
        assert restart.executor_submissions == 0 and restart.guard_hits == 1
    finally:
        restart.close()
    return {
        "outcomes": outcomes,
        "three_fuse_elapsed_ms": measured,
        "discovery_retained_entries": len([topic for topic in broker.retained if topic.endswith("/config")]),
        "trip3_submissions_after_guard": 0,
        "trip3_restart_submissions": 0,
        "exit_wait_at_6s": 4.1,
    }


async def test_stability(directory: Path) -> dict:
    store = AtomicStatus(directory)
    store.increment_fuse("previous-1")
    store.increment_fuse("previous-2")
    broker = MockRetainedBroker()
    ha = MockHA(broker)
    clock = ManualClock(500.0)
    valid = HostCounterDispatcherModel(
        FakeDriver(), ValidJKBMS(), ha, store, clock, process_age=30.0, decode_timeout=0.2
    )
    try:
        await valid.process_once()
        assert valid.valid_jkbms_since == 500.0
        clock.advance(899.9)
        assert not valid.tick() and store.read()["trip_count"] == 2
        clock.advance(0.1)
        assert valid.tick() and store.read() == AtomicStatus.default()
    finally:
        valid.close()

    store.increment_fuse("previous-again")
    clock = ManualClock(1000.0)
    invalidated = HostCounterDispatcherModel(
        FakeDriver(), ValidJKBMS(), ha, store, clock, process_age=30.0, decode_timeout=0.2
    )
    try:
        await invalidated.process_once()
        assert invalidated.valid_jkbms_since == 1000.0
        invalidated.adapter = BoundedHangingJKBMS()
        invalidated.decode_timeout = 0.005
        for _ in range(1):
            await invalidated.process_once()
        assert invalidated.valid_jkbms_since is None
        clock.advance(901.0)
        assert not invalidated.tick()
        assert store.read()["trip_count"] == 1
    finally:
        invalidated.close()
    return {"reset_boundary_s": [899.9, 900.0], "timeout_invalidates": True, "counter_after_timeout": 1}


def test_atomic_concurrency(directory: Path) -> dict:
    process_count, increments = 8, 25
    processes = [
        multiprocessing.Process(target=_increment_worker, args=(str(directory), increments))
        for _ in range(process_count)
    ]
    started = time.monotonic()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    store = AtomicStatus(directory)
    state = store.read()
    assert state["trip_count"] == process_count * increments
    assert not list(directory.glob("*.tmp"))
    json.loads(store.path.read_text(encoding="utf-8"))
    return {"processes": process_count, "increments_each": increments,
            "expected": process_count * increments, "actual": state["trip_count"], "elapsed_ms": elapsed_ms}


async def test_safe_status_errors(directory: Path) -> dict:
    broker = MockRetainedBroker()
    ha = MockHA(broker)

    corrupt = AtomicStatus(directory / "corrupt")
    corrupt.directory.mkdir(parents=True, exist_ok=True)
    corrupt.path.write_text("{not-json", encoding="utf-8")
    corrupt_model = HostCounterDispatcherModel(
        FakeDriver(), BoundedHangingJKBMS(), ha, corrupt, ManualClock(), process_age=20.0
    )
    try:
        assert corrupt_model.decoding_disabled and corrupt_model.safe_error.startswith("status_load_failed:")
        await corrupt_model.process_once()
        assert corrupt_model.executor_submissions == 0
        assert broker.retained[ha.status_topic] == "offline"
    finally:
        corrupt_model.close()

    unwritable = AtomicStatus(directory / "unwritable")
    initial = unwritable.read()
    assert initial["trip_count"] == 0
    unwritable.path.mkdir()  # Existing status path is now the wrong OS type; next update really fails.
    write_model = HostCounterDispatcherModel(
        FakeDriver(), BoundedHangingJKBMS(), ha, unwritable, ManualClock(), process_age=20.0
    )
    # Construction sees the bad path and locks before it can submit, which is the safe result.
    try:
        assert write_model.decoding_disabled and write_model.safe_error.startswith("status_load_failed:")
        await write_model.process_once()
        assert write_model.executor_submissions == 0
    finally:
        write_model.close()

    # A separate actual write failure after a successful load exercises the fuse branch.
    late = AtomicStatus(directory / "late-write-failure")
    late_model = HostCounterDispatcherModel(
        FakeDriver(), BoundedHangingJKBMS(), ha, late, ManualClock(), process_age=20.0
    )
    late.path.mkdir()  # Cannot replace a directory with the status JSON.
    try:
        for _ in range(3):
            await late_model.process_once()
        assert late_model.decoding_disabled
        assert late_model.exit_code is None
        assert late_model.safe_error and late_model.safe_error.startswith("status_write_failed:")
    finally:
        late_model.close()
    return {"corrupt_locks_before_submit": True, "wrong_type_locks_before_submit": True,
            "write_failure_locks_without_exit": True}


def test_docker_arming() -> dict:
    waits = {age: max(0.0, ARM_SECONDS - age) for age in (0.0, 6.0, 10.0, 10.1, 12.0)}
    expected = {0.0: 10.1, 6.0: 4.1, 10.0: 0.1, 10.1: 0.0, 12.0: 0.0}
    assert all(math.isclose(waits[age], value, abs_tol=1e-9) for age, value in expected.items())
    return {"arm_seconds": ARM_SECONDS, "waits": {age: round(value, 3) for age, value in waits.items()}}


def run_case(results: dict, name: str, callback) -> None:
    started = time.monotonic()
    detail = callback()
    results[name] = {"pass": True, "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "detail": detail}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {"tests": {}}
    with tempfile.TemporaryDirectory(prefix="run-", dir=OUT) as temp_root:
        root = Path(temp_root)
        run_case(results["tests"], "static_baseline", static_baseline)
        run_case(results["tests"], "atomic_concurrency", lambda: test_atomic_concurrency(root / "atomic"))
        run_case(results["tests"], "trip_ladder", lambda: asyncio.run(test_trip_ladder(root / "trips")))
        run_case(results["tests"], "stability_900s", lambda: asyncio.run(test_stability(root / "stability")))
        run_case(results["tests"], "docker_arming", test_docker_arming)
        run_case(results["tests"], "safe_status_errors", lambda: asyncio.run(test_safe_status_errors(root / "errors")))
    results["summary"] = {"total": len(results["tests"]), "passed": len(results["tests"]), "failed": 0}
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"validation_017: PASS ({results['summary']['passed']}/{results['summary']['total']})")


if __name__ == "__main__":
    main()
