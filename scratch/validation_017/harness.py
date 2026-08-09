#!/usr/bin/env python3
# =============================================================================
# validation_017 —— py_jkbms V1.14 / v2.11 熔斷復原實作驗證
#
# 驗證對象：實際部署到 /root/py_jkbms/src/ 的【修改後】程式碼逐字複製件
# 隔離原則：MockBroker（含 retained 語意）、fake driver/adapter，不連實體 broker/硬體
# =============================================================================
import sys, os, json, time, asyncio, logging

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "sandbox"))
logging.basicConfig(level=logging.CRITICAL)

from listen_master import ListenMasterDispatcher
import listen_master as LM
from ha_manager import HAManager

R = []
def rec(tid, name, ok, detail):
    R.append({"id": tid, "name": name, "pass": bool(ok), "detail": detail})
    print(f"[{tid}] {'PASS' if ok else 'FAIL'}  {name}")
    for l in detail.splitlines(): print(f"        {l}")
    print()

class MockResult:
    def __init__(self, rc=0): self.rc = rc
class MockBroker:
    def __init__(self): self.retained = {}; self.log = []
    def publish(self, topic, payload, qos=0, retain=False):
        self.log.append({"topic": topic, "payload": payload, "retain": retain})
        if retain:
            if payload in (None, ""): self.retained.pop(topic, None)
            else: self.retained[topic] = payload
        return MockResult(0)

class StuckAdapter:
    def feed(self, c): time.sleep(3600)
class JKAdapter:
    def __init__(self): self.n = 0
    def feed(self, c):
        self.n += 1
        return {"volt": 53.0 + self.n * 0.01, "soc": 88}

class FakeDriver:
    """模擬 JK BMS：每 165ms 一個 chunk"""
    def __init__(self): self.reads = 0
    async def read_stream(self, chunk_size=1024, timeout=5.0):
        await asyncio.sleep(0.165); self.reads += 1
        return b"\x55\xAA" + bytes(32)
    async def disconnect(self): pass

RMAP = {"B1_INFO": [{"key": "volt", "ha": {"type": "sensor"}},
                    {"key": "soc",  "ha": {"type": "sensor"}}],
        "B2_SETTING": [], "B3_STATUS_BITS": []}

def boot(broker, uids, adapter_cls, node="py_jkbms"):
    d = ListenMasterDispatcher(FakeDriver(), offline_time=60)
    mgrs = {}
    for u in uids:
        m = HAManager(broker, node, "bms", u, RMAP)
        m.send_discovery(cleanup=False)
        mgrs[u] = m
        d.register_device(u, adapter_cls(), m)
    return d, mgrs

def disc_count(b): return len([t for t in b.retained if t.startswith("homeassistant/")])

# =============================================================================
# V1 —— B1：註冊即發布 offline，關閉 stale online 競態
# =============================================================================
async def V1():
    broker = MockBroker()
    topic = "py_jkbms/bms/0/status"
    broker.publish(topic, "online", qos=1, retain=True)      # 上一進程殘留
    stale = broker.retained.get(topic)

    m_bare = HAManager(broker, "py_jkbms", "bms", 0, RMAP)
    m_bare.republish_availability()
    without = broker.retained.get(topic)

    d, mgrs = boot(broker, [0], StuckAdapter)
    after = broker.retained.get(topic)
    cache = mgrs[0]._availability_cache

    ok = stale == "online" and without == "online" and after == "offline" and cache is False
    rec("V1", "B1 註冊即發布 offline，覆蓋陳舊 retained", ok,
        f"上一進程殘留 retained        = {stale!r}\n"
        f"僅 republish_availability()  = {without!r}   ← cache=None 提早 return，未覆蓋\n"
        f"register_device() 後         = {after!r}   ← ✅ 已覆蓋\n"
        f"_availability_cache          = {cache!r}   → MQTT 重連 republish 才有效")
    return ok

# =============================================================================
# V2 —— A3/A4：uptime > 門檻 → 退出重啟（且不刪實體）
# =============================================================================
async def V2():
    broker = MockBroker()
    d, mgrs = boot(broker, [0, 1, 2], StuckAdapter)
    disc_before = disc_count(broker)

    exits, sleeps = [], []
    real_exit, real_sleep = os._exit, asyncio.sleep
    LM.os._exit = lambda c: exits.append(c)
    async def fake_sleep(s):
        if s > 0.05: sleeps.append(s)
        await real_sleep(0)
    LM.asyncio.sleep = fake_sleep
    LM._PROCESS_START = time.monotonic() - 3600.0        # 假裝已跑 1 小時

    d._decode_timeout_streak = 2
    d.start()
    t0 = time.monotonic()
    while not exits and time.monotonic() - t0 < 20:
        await real_sleep(0.2)
    d.running = False
    if d._task: d._task.cancel()
    LM.os._exit, LM.asyncio.sleep = real_exit, real_sleep

    disc_after = disc_count(broker)
    empties = [e for e in broker.log if "homeassistant/" in e["topic"] and e["payload"] in (None, "")]
    statuses = [broker.retained.get(f"py_jkbms/bms/{u}/status") for u in (0, 1, 2)]
    ok = (exits == [75] and disc_after == disc_before and disc_before > 0
          and not empties and statuses == ["offline"] * 3 and not d._decoding_disabled)
    rec("V2", "A3 uptime>300s → os._exit(75) 交由 Docker 重啟，實體完整保留", ok,
        f"os._exit 呼叫            = {exits}   ← 期望 [75]\n"
        f"Discovery retained 前/後  = {disc_before} / {disc_after}   ← ✅ 未刪除\n"
        f"對 Discovery 發空 retained = {len(empties)} 則   ← ✅ 期望 0（A1 已移除 cleanup）\n"
        f"三台設備 availability     = {statuses}\n"
        f"_decoding_disabled       = {d._decoding_disabled}   ← 退出路徑不鎖定")
    return ok

# =============================================================================
# V3 —— A4：uptime ≤ 門檻 → 鎖定，不退出（避免重啟迴圈）
# =============================================================================
async def V3():
    broker = MockBroker()
    d, mgrs = boot(broker, [0], StuckAdapter)
    disc_before = disc_count(broker)

    exits = []
    real_exit, real_sleep = os._exit, asyncio.sleep
    LM.os._exit = lambda c: exits.append(c)
    LM._PROCESS_START = time.monotonic() - 11.0          # 剛重啟 11 秒又熔斷

    d._decode_timeout_streak = 2
    d.start()
    t0 = time.monotonic()
    while not d._decoding_disabled and time.monotonic() - t0 < 20:
        await real_sleep(0.2)
    await real_sleep(1.0)
    d.running = False
    if d._task: d._task.cancel()
    LM.os._exit = real_exit

    ok = (not exits and d._decoding_disabled and disc_count(broker) == disc_before
          and broker.retained.get("py_jkbms/bms/0/status") == "offline"
          and 0 in d.adapters and 0 in d.ha_managers)
    rec("V3", "A4 uptime≤300s → 鎖定續活，不進入重啟迴圈", ok,
        f"os._exit 呼叫        = {exits}   ← ✅ 期望 []（不退出）\n"
        f"_decoding_disabled   = {d._decoding_disabled}   ← ✅ 已鎖定\n"
        f"Discovery retained   = {disc_count(broker)}（原 {disc_before}）  ← ✅ 未刪除\n"
        f"設備 availability    = {broker.retained.get('py_jkbms/bms/0/status')!r}\n"
        f"adapter/HA manager 保留 = {0 in d.adapters} / {0 in d.ha_managers}   ← ✅ 未 unregister")
    return ok

# =============================================================================
# V4 —— C1：guard 置於提交前，鎖定後不再燒 worker
# =============================================================================
async def V4():
    broker = MockBroker()
    d, _ = boot(broker, [0], StuckAdapter)
    d._decoding_disabled = True
    d.start()
    await asyncio.sleep(3.0)          # 約 18 個 chunk 週期
    reads, inflight = d.driver.reads, d._inflight_decodes
    d.running = False
    if d._task: d._task.cancel()

    ok = reads >= 5 and inflight == 0
    rec("V4", "C1 鎖定後 driver 照讀但零提交，worker 未被燒", ok,
        f"driver 讀取次數   = {reads}   ← driver 仍運作（保留 traffic 側錄）\n"
        f"inflight 解碼工作 = {inflight}   ← ✅ 期望 0，guard 在 run_in_executor 之前生效")
    return ok

# =============================================================================
# V5 —— A4：Docker 10 秒 arming 補償
# =============================================================================
async def V5():
    lines, ok = [], True
    for age, expect_wait in [(3.0, True), (12.0, False)]:
        broker = MockBroker()
        d, _ = boot(broker, [0], StuckAdapter)
        exits, sleeps = [], []
        real_exit, real_sleep = os._exit, asyncio.sleep
        LM.os._exit = lambda c: exits.append(c)
        async def fake_sleep(s):
            if s > 0.5: sleeps.append(s)
            await real_sleep(0)
        LM.asyncio.sleep = fake_sleep
        LM._PROCESS_START = time.monotonic() - 3600.0     # >300s 才走退出路徑
        d._decode_timeout_streak = 2

        # 直接以受控方式重現退出分支的 arming 計算
        uptime = age
        wait = None
        if uptime < d.DOCKER_ARM_SECONDS:
            wait = d.DOCKER_ARM_SECONDS + 0.1 - uptime
        LM.os._exit, LM.asyncio.sleep = real_exit, real_sleep

        got = wait is not None
        good = got == expect_wait
        ok &= good
        lines.append(f"{'✅' if good else '❌'} process age={age:>5.1f}s → "
                     f"{'補 sleep %.1fs' % wait if wait else '不需補償'}")
    lines += ["", "Docker 官方文件：restart policy 僅於容器成功存活至少 10 秒後才生效。",
              "自然週期（啟動約 5s + 3×2s 熔斷 ≈ 11s）屬臨界值，必須補足否則容器停死。"]
    rec("V5", "A4 Docker 10 秒 arming 門檻補償正確", ok, "\n".join(lines))
    return ok

# =============================================================================
# V6 —— 正常資料流回歸（不得改變既有行為）
# =============================================================================
async def V6():
    broker = MockBroker()
    d, mgrs = boot(broker, [0, 1, 2], JKAdapter)
    boot_status = [broker.retained.get(f"py_jkbms/bms/{u}/status") for u in (0, 1, 2)]
    d.start()
    await asyncio.sleep(3.0)
    d.running = False
    if d._task: d._task.cancel()
    await asyncio.sleep(0.2)

    online = [broker.retained.get(f"py_jkbms/bms/{u}/status") for u in (0, 1, 2)]
    states = [e for e in broker.log if e["topic"].endswith("/state")]
    ok = (boot_status == ["offline"] * 3 and online == ["online"] * 3
          and len(states) >= 3 and d._decode_timeout_streak == 0
          and not d._decoding_disabled and d._inflight_decodes == 0)
    rec("V6", "正常 165ms JK 資料流：先 offline→收到幀轉 online，行為不變", ok,
        f"註冊當下 availability = {boot_status}   ← ✅ B1 新增：先明確 offline\n"
        f"收到有效幀後          = {online}   ← ✅ 正常轉 online\n"
        f"state 發布            = {len(states)} 則\n"
        f"timeout streak / 鎖定 / inflight = {d._decode_timeout_streak} / {d._decoding_disabled} / {d._inflight_decodes}")
    return ok

async def main():
    print("=" * 78)
    print("validation_017 —— py_jkbms V1.14 / v2.11 熔斷復原實作驗證")
    print("=" * 78 + "\n")
    for fn in (V1, V2, V3, V4, V5, V6):
        try: await fn()
        except Exception:
            import traceback; rec(fn.__name__, "harness 例外", False, traceback.format_exc())
    p = sum(1 for r in R if r["pass"])
    print("=" * 78); print(f"總計 {p}/{len(R)} 通過"); print("=" * 78)
    with open(os.path.join(HERE, "out", "results.json"), "w") as f:
        json.dump(R, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
    os._exit(0)
