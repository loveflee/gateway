# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Edge Gateway" (internal name `py_ginlong`) — an asyncio Python industrial IoT gateway that polls Modbus RTU/TCP devices (solar inverters, BMS packs, relays, sensors) over RS485/RS232/TCP and bridges them to Home Assistant via MQTT Discovery. Runs as a single long-lived process inside Docker, plus an embedded FastAPI WebUI for live config editing and bus debugging. No test suite, no linter config, no package manifest — this is a deployed appliance, not a library.

## Commands

There is no local Python dev loop; everything runs through Docker.

- `./build.sh` — full rebuild: `docker compose build --no-cache && down && up -d`, then tails logs
- `./up.sh` — recreate containers from current image and tail logs
- `./down.sh` — stop the stack
- `./restart.sh` — `docker restart ginlong` and tail logs (fast path, no rebuild — use after editing files under the bind-mounted `src/`, `profile/`, `adapters/` dirs)
- `./log.sh` — tail container logs
- `./it.sh` — shell into the running container

Validate a device profile/map YAML in isolation before mounting it (PyYAML is not installed on the host, so run it inside the container):
```
docker exec ginlong python /app/src/map_validator.py /app/profile/solis_inverter_map.yaml
```
This is the same validator `main.py` runs at startup for every configured device — a profile that fails validation is logged and skipped, it does not crash the gateway. The CLI exits non-zero and prints every error found.

## Docker/volume layout (important when reasoning about paths)

`docker-compose.yaml` bind-mounts `./src` → `/app/src:ro`, `./profile` → `/app/profile:rw`, `./adapters` → `/app/src/adapters:ro`, and sets the container's **working directory to `/app/profile`**. This is why `main.py` loads `config.yaml` and profile YAMLs with bare relative filenames — they resolve against `/app/profile` at runtime, not against `src/`. `network_mode: host` is used deliberately (this is an OT gateway talking to LAN devices). WebUI credentials (`WEB_USER`/`WEB_PASS`) and MQTT credentials (`MQTT_USERNAME`/`MQTT_PASSWORD`) are supplied from the root `.env` file; `WEB_PORT` remains a non-secret Compose environment variable.

Two consequences that bite:
- **Adapters exist only via the bind mount.** The `Dockerfile` only `COPY`s `src/`; the repo's `src/adapters/` is an empty directory. The image itself has no adapters — `./adapters` is mounted over `/app/src/adapters` at run time. Never "fix" this by copying adapters into `src/adapters/`; the mount would shadow them anyway.
- Because `src/`, `profile/`, and `adapters/` are all bind-mounted, `./restart.sh` picks up any Python or YAML edit. `./build.sh` is only needed when `requirements.txt` or the `Dockerfile` changes.

## config.yaml shape

Lives at `profile/config.yaml` (mounted rw — the WebUI writes it). Top-level blocks: `system` (`node_id`, `log_level`, `enable_sniffer`), `mqtt`, `driver` (active track), `listen_driver` (listen track, only needed if some device is `mode: listen`), `bus` (`offline_time`, default 60), and `devices`.

Each `devices` entry: `uid` (Modbus slave id, also the MQTT path segment), `device_type` (free-form, becomes an MQTT topic segment), `adapter`, `profile` (filename without `.yaml`), `poll_interval` (seconds, default 10), `mode` (`active`|`listen`).

- `adapter:` must match a module's `ADAPTER_NAME`, **not** its filename. Currently registered: `rtu` (`generic_adapter.py`), `tcp` (`modbus_tcp_adapter.py`), `jkbms`, `st_inverter`. Note `main.py` defaults a missing `adapter:` key to `"generic"`, which nothing registers — an entry without an explicit `adapter:` is silently skipped with a logged error.
- Everything under `driver:` except `type` is splatted as kwargs into the driver class, so a typo'd key is a fatal `TypeError` at startup rather than an ignored setting.
- `_load_profile` tries `{profile}.yaml` first and falls back to `importlib.import_module(profile)` — profiles *can* be Python modules, though every shipped one is YAML.

### What is fatal vs. what is quarantined

`sys.exit(1)` at startup: unreadable `config.yaml`, a missing required field (`mqtt.broker`, `driver.host`/`port`, `driver.port`/`baudrate` for usb), an unknown `driver.type`, bad driver kwargs, driver connect failure, `mode: listen` devices with no `listen_driver:` block, and the active/listen port collision. Per-device skip + log (gateway keeps running): unknown adapter name, profile validation failure, adapter constructor raising, and an adapter missing its mode's required methods.

## Architecture

### Two parallel bus tracks, mutually exclusive per port

`EdgeGateway` (`src/main.py`) reads `config.yaml` and wires up to two independent tracks depending on each device's `mode`:

- **Active track** (`mode: active`, default): `driver.py`/`modbus_tcp_driver.py`/`local_serial_driver.py` do request/response I/O; `bus_master.py`'s `BusMasterScheduler` arbitrates a single shared bus between scheduled polls (a min-heap keyed by next-poll-time) and queued writes (fast lane, budget-limited so writes can't starve polling).
- **Listen/passive track** (`mode: listen`): `listen_driver.py` just reads raw byte chunks off the wire (no requests sent); `listen_master.py`'s `ListenMasterDispatcher` fans each chunk out to every registered adapter's `feed()` method in a thread pool, diffs decoded values against a cache, and only publishes on change. Read-only by design — writes to a `listen` UID are rejected in `main.py`'s MQTT command handler.

Active and listen devices cannot share the same physical port (`driver.port` == `listen_driver.port` is a fatal startup check) — that's a real electrical/protocol collision, not just a config nicety.

#### Active track scheduling semantics (`bus_master.py`)

Non-obvious behaviours that change how you read logs and tune config:

- **`poll_interval` is per *command*, not per device refresh.** `build_poll_read()` returns exactly one entry from `read_commands` and advances a round-robin index. A profile with 5 `read_commands` at `poll_interval: 15` takes 75s to refresh every sensor once. Splitting a map into more `read_commands` slows the whole device down proportionally.
- **Offline/online is hysteretic**: 5 consecutive failures → OFFLINE + `set_availability(False)` + reschedule at `bus.offline_time` (slow probe); 2 consecutive successes → back ONLINE. A single timeout does not mark a device down.
- **Writes are write-then-verify-readback.** `_process_write` sends `encode_write()`, then immediately issues `build_verify_read(key)` and compares the decoded value to what was written (float tolerance 0.01). Up to 3 attempts, or 1 if the device is already offline (fast-fail). A driver `write()` returning `False` means the *device* logically rejected it — no retry.
- Exhausting retries on **physical** faults counts as a failure; exhausting them on **value mismatch** does not — the device stays ONLINE and it's logged as a hardware limitation (some registers simply don't read back what you wrote).
- `pending_writes` is a dict keyed by `(uid, key)`, so repeated writes to the same key **coalesce to the last value** before dispatch. Cap 200; beyond that, new keys are dropped.
- Write fast-lane budget: after 5 consecutive writes the scheduler forces a poll, so a write flood cannot starve polling. All bus access is serialized under `bus_lock`.

#### Listen track safeguards (`listen_master.py`)

Adapter `feed()` runs in a 4-thread pool with a 2s timeout. Three consecutive decode timeouts blow a **software fuse**: all listen devices are forced offline and unregistered (calling back into `app_state.gateway.unregister_device`) to stop OS thread leakage. If listen devices vanish from HA en masse, look for that CRITICAL log — the cause is a hanging `feed()`, not the network. Per-device diff cache is an LRU `OrderedDict` capped at 500 keys; frames with >256 keys and string values >256 chars are discarded.

**Sniffer mode** (`system.enable_sniffer: true` in config.yaml) is a third state: normal polling and listening are fully suspended, any listen driver is released, and the WebUI (`/api/sniffer/send`) gets exclusive access to the raw driver to send arbitrary hex frames and see the response — used for interactively reverse-engineering a device's register map. Look at `EdgeGateway.start()`'s sniffer branch and `web_admin.py`'s `/api/sniffer/*` routes together to follow this flow.

### Driver layer (protocol-blind byte pipes)

`driver.py`'s `RobustAsyncTcpDriver` (`type: rtu` in config — TCP-to-RS485 serial-server) and its subclass `modbus_tcp_driver.py`'s `AsyncModbusTcpDriver` (`type: tcp` — native Modbus TCP), plus `local_serial_driver.py`'s `LocalSerialDriver` (`type: usb` — direct USB/RS485 dongle), all expose the same duck-typed contract: `connect()`, `disconnect()`, `read(payload) -> bytes`, `write(payload) -> bool`. They are deliberately protocol-blind — they send/receive raw bytes, enforce inter-frame delay, flush stale buffers, and self-heal on timeout/disconnect, but never parse Modbus semantics. All Modbus interpretation (exception codes, CRC, register decode) lives in the adapter layer. `DRIVER_FACTORY` in `main.py` maps `driver.type` → driver class.

### Adapter plugin system

Any `adapters/*_adapter.py` file is auto-discovered by `load_adapters()` in `main.py` (via `pkgutil.iter_modules`, only top-level modules ending in `_adapter`, not packages). To be loaded, a module must define module-level `ADAPTER_NAME` (registry key, lowercased) and a class named exactly `Adapter`. Name collisions and missing-attribute modules are rejected with a logged error, not silently ignored.

Adapter contract, enforced by `main.py` at device-mount time:
- **Active mode** adapters need `build_poll_read()`, `build_verify_read(key)`, `encode_write(key, value)`, `decode(raw_bytes, context)`.
- **Listen mode** adapters need `feed(chunk) -> dict|None`.

`adapters/generic_adapter.py` (`ADAPTER_NAME = "rtu"`) is the reference implementation: builds Modbus RTU frames with CRC16, does deep noise-tolerant frame scanning in `decode()` (searches for a valid `[uid][fc]...[crc]` window inside a noisy buffer rather than trusting frame boundaries), and handles all the numeric edge cases (word-swap variants, signed/unsigned, 16/32/64-bit, scale factors, bit-field extraction, value-map lookups). `adapters/modbus_tcp_adapter.py` (`ADAPTER_NAME = "tcp"`) subclasses it and only overrides framing to add/strip the MBAP header. `adapters/jkbms_adapter.py` and `adapters/st_inverter_adapter.py` are other concrete adapters (BMS and a second inverter family respectively) — use `generic_adapter.py` as the template when adding a new device family rather than the others, since they carry device-specific quirks.

`adapters/bak.generic_adapter` and `adapters/new.generic_adapter` are backup/WIP snapshots (don't end in `_adapter.py`, so the loader never picks them up) — don't treat them as live code, but check them before assuming a change to `generic_adapter.py` is novel.

### Device profiles (`profile/*.yaml` — the "map" files)

Each profile is split into a backend half and a frontend half:
- **Backend** (`read_commands`, `sensors`, `settings`, `definitions.value_maps`): raw register geometry — offsets, lengths, datatypes, scale factors, function codes — consumed by the adapter's encode/decode logic.
- **Frontend** (`B1_INFO`, `B2_SETTING`, `B3_STATUS_BITS`): declares Home Assistant entities (`ha: {type: sensor|binary_sensor|switch|number|select|button|text, ...}`) that `ha_manager.py` turns into MQTT Discovery payloads. Frontend and backend keys share the same namespace and must not collide across a profile.

Every profile is run through `src/map_validator.py` before its adapter is instantiated (see `EdgeGateway.start()`); validation failures quarantine that single device (skip + log) rather than aborting the whole gateway. When editing `map_validator.py`, keep the "fail loud, never silently coerce" posture — it exists specifically to keep malformed YAML from reaching `struct.pack` in the adapter layer.

The backend halves have **different shapes**, which is easy to get wrong:

- `read_commands` — list of `{id, fc, start_addr, count, response_len}`. Frames are pre-built once at adapter construction.
- `sensors` — list, each entry tied to a command by `command_id`, positioned by `offset`/`length`, decoded per `datatype`/`word_order`/`scale`, or expanded into per-bit entities via a `bits:` list.
- `settings` — a **dict keyed by register address** (hex string like `"0x603"` or an int), each value `{key, write_fc, scale, link_sensor, verify_count}`. Note the key/value inversion relative to `sensors`.

Three semantics that are not guessable from the YAML:

- **`offset` is a byte offset into the raw response frame, not a register index.** The first data byte of a standard `[uid][fc][bytecount]…` response is offset 3. The bounds check is against `len(raw_data) - 2` (CRC excluded).
- **`scale` is a divisor on read and a multiplier on write** (`val / scale` decoding, `round(value * scale)` encoding) — so it round-trips, but it is not the "multiply by 0.1" convention some vendor docs use.
- **`map_profile` value maps are bidirectional**: raw→label on read, and reverse-looked-up label→raw on write (via the setting's `link_sensor`). Strings `ON`/`TRUE`/`OFF`/`FALSE` are additionally coerced to 1/0.

`generic_adapter` writes with FC 5/6/16 and reads back with FC 1/2/3/4 (coil writes verify via FC1, register writes via FC3). Anything else raises `NotImplementedError`.

On the frontend half, `ha.state_key` (or `link_b1` for `select`/`text`) repoints an entity's `value_template` at a *different* key in the state JSON — this is how a writable `B2_SETTING` entity displays its `B1_INFO` readback. `unit` values `Hex`, `Bit`, and `Enum` are sentinels meaning "no unit" and are deliberately not emitted as `unit_of_measurement`.

### MQTT / Home Assistant integration

`mqtt_client.py`'s `RobustMQTTClient` wraps paho-mqtt v2 (thread-based client) and bridges inbound messages into the asyncio world via `EdgeGateway._on_mqtt_message` → `_cmd_queue` (bounded at 500, load-shed on overflow — a flooding MQTT broker cannot back up into the gateway). `ha_manager.py`'s `HAManager` (one instance per device UID) owns Discovery payload construction per HA domain and availability publishing.

Full topic surface:

| Topic | Direction | Notes |
|---|---|---|
| `{node_id}/status` | out, retained | Gateway availability, also the LWT payload |
| `{node_id}/health` | out | JSON every 60s: uptime, `cmd_queue_size`, per-UID online/timeout/success counts and mode |
| `{node_id}/{device_type}/{uid}/state` | out | One merged JSON blob per device, not per-key |
| `{node_id}/{device_type}/{uid}/status` | out, retained | Per-device availability |
| `{node_id}/{device_type}/{uid}/set/{key}` | in | Write command |
| `{node_id}/system/set/restart` | in | Triggers a graceful shutdown |
| `{discovery_prefix}/{domain}/{node_id}_{device_type}_{uid}/{key}/config` | out, retained | Discovery; an empty payload here is the cleanup/erase |

Details that matter:

- Availability is **dual-topic** with `availability_mode: all` — an entity is only available when both the gateway topic and its device topic say `online`. A device looking permanently unavailable in HA usually means one of the two is stale-retained.
- Every device automatically gets an extra `connectivity` binary_sensor that is **not** declared in the profile — `connectivity` is effectively a reserved key.
- Unmounting a device publishes empty retained payloads to its Discovery topics (`send_discovery(cleanup=True)`) to avoid zombie entities. Anything that removes a device must go through `unregister_device`, not just drop it from a dict.
- `publish_state` throttles to one publish per 200ms and `set_availability` to one per second, but the state cache still merges in the meantime — so a burst produces one publish carrying all of it, not dropped data. State cache is capped at 500 keys per device.
- The gateway restart path is "die and let Docker restart us": both `{node_id}/system/set/restart` and the WebUI's `/api/restart` end in a `SIGTERM` to self, relying on compose's `restart: unless-stopped`.

### WebUI (`web_admin.py` + `src/index.html`)

FastAPI app run in a background thread (`start_webui`, daemon thread started from `main.py`'s `__main__` block) alongside the asyncio main loop; cross-thread calls into the gateway (e.g. sniffer send) use `asyncio.run_coroutine_threadsafe` against the stored event loop, and the live `EdgeGateway` instance is shared via the `app_state` module-level global (`app_state.gateway`) rather than dependency injection. HTTP Basic Auth on every route.

Config writes (`/api/config`) are atomic (`tmp` file + `os.replace` + `fsync` on both file and containing dir), hold an exclusive `fcntl.flock` on `config.yaml.lock` for the duration, and always snapshot the previous `config.yaml` to `config.yaml.bak` first; `/api/restore` reverts from that backup under the same lock. The three files `config.yaml`, `config.yaml.bak`, and the zero-byte `config.yaml.lock` are all runtime state living in `profile/` — don't hand-edit `.bak` or delete the lock file.

`/api/sniffer/send` is guarded by a non-blocking `threading.Lock` (one in-flight frame at a time, second caller gets an error rather than queueing), optionally appends CRC16 by importing `calc_crc16` from `generic_adapter`, and waits `driver.timeout + 2s`. It refuses unless the gateway is actually in sniffer mode, and prefers `gw._sniffer_driver` over `gw.driver`.

`app_state.traffic_log` (a bounded `deque`) is a lazily-imported, best-effort sink that `driver.py`/`bus_master.py`/`listen_driver.py` push hex dumps into for the WebUI's live bus-traffic view, tagged `[Poll-TX]`/`[Poll-RX]`/`[Write-TX]`/`[Verify-TX]`/`[Verify-RX]`. It's optional plumbing — the lazy import and silent failure are deliberate, so failures there never affect the I/O path. Keep that property when touching it.

## Conventions worth knowing before editing

- Source files carry a version header comment (`# version foo.py - VX.Y ...`) with a changelog of fixes — bump it and add a line when you make a non-trivial change, consistent with existing history.
- Comments and log messages are predominantly Traditional Chinese; match that when editing existing files.
- The codebase leans hard into defensive programming at every boundary (device I/O, YAML parsing, MQTT payloads) — type-coerce and validate at the edge, then trust the value everywhere downstream. Follow that pattern rather than adding ad hoc checks deeper in the call chain.
- `scratch/` holds one-off debugging/probing scripts (register scanners, dump parsers, a vendored `solis_modbus` reference project) — not part of the running application, safe to ignore unless a task explicitly references one.
- Stray artefacts at the repo root and in `profile/`: `log2` and `usb_log` are captured log dumps, `profile/solis_inverter_map.err` is a rejected profile kept for reference, `profile/js-yaml.min.js` is vendored and served by the WebUI at `/api/js-yaml.min.js`. None are live code.

## Reference material for the Solis hardware

`Solis_Inverter_Modbus_Dev_Notes.md` (repo root) and `profile/錦浪.md` (the vendor's Ver3.7 RS485 Modbus spec) are the authority for the inverter register maps. Read the dev notes before tuning timing or debugging "the write succeeded but nothing changed" — they document hardware quirks the code is deliberately shaped around:

- Reads need >300ms spacing, writes >700ms, or the inverter's comms module stops responding. This is what `driver.inter_frame_delay: 0.35` in `config.yaml` is compensating for; don't lower it to "speed up polling".
- Reading an unimplemented register returns `0x0000` rather than a Modbus exception, and writing one is silently discarded while still returning a normal ACK. So a decoded 0 is ambiguous, and a successful write ACK proves nothing — this is exactly why `bus_master` does write-then-verify-readback instead of trusting the ACK.
