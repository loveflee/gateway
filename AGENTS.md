# Repository Guidelines

## Project Structure & Module Organization

This is an asyncio industrial IoT gateway that polls Modbus devices and publishes to Home Assistant through MQTT. Runtime code is in `src/`: `main.py` wires the gateway, drivers, MQTT, scheduling, validation, and Web UI together. Adapter plugins live in `adapters/`; only top-level `*_adapter.py` modules are discovered. Device and Home Assistant maps are in `profile/*.yaml`. Treat `scratch/` as disposable investigation material and `report/` as project notes, not production code. The running container bind-mounts `src/`, `adapters/`, and `profile/`.

## Build, Test, and Development Commands

- `./build.sh` rebuilds the Docker image without cache, restarts the stack, and tails logs. Use it after changing `Dockerfile` or `requirements.txt`.
- `./restart.sh` restarts the running `ginlong` container and tails logs. It picks up edits to mounted Python, adapter, and profile files.
- `./up.sh`, `./down.sh`, and `./log.sh` start, stop, and follow the service.
- `./it.sh` opens a shell in the running container.
- Validate a profile before deployment: `docker exec ginlong python /app/src/map_validator.py /app/profile/solis_inverter_map.yaml`.

There is currently no automated test suite. Exercise changes against representative hardware or a safe test environment, inspect container logs, and validate every changed YAML map.

## Coding Style & Naming Conventions

Use four-space Python indentation and standard `snake_case` for functions, variables, and modules; classes use `PascalCase`. Keep adapter filenames in the `*_adapter.py` pattern and define both `ADAPTER_NAME` and `Adapter`. Match the surrounding code’s defensive validation, logging style, and predominantly Traditional Chinese comments/messages. For non-trivial source changes, update the file’s version header and changelog entry.

## Configuration & Safety

`profile/config.yaml` is runtime configuration and can be changed by the Web UI. Do not edit `config.yaml.bak` or remove `config.yaml.lock`. Preserve device timing and validate register offsets, scales, and write verification carefully: this software controls live OT equipment. Keep `WEB_USER`/`WEB_PASS` and `MQTT_USERNAME`/`MQTT_PASSWORD` only in the root `.env`; do not add credentials to source, Compose, or profile files.

## Commit & Pull Request Guidelines

This checkout has no readable Git history, so no repository-specific commit convention can be confirmed. Use concise imperative commits, for example `Validate malformed device profiles`. Keep each commit focused. Pull requests should describe operational impact, list changed profiles/adapters, include validation commands and relevant logs, link the issue when applicable, and include Web UI screenshots for visible interface changes.
