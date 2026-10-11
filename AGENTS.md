# Working notes for AI agents / the Base44 sandbox

## What this repo is

Bitcoin Core: a C++ daemon (`bitcoind`) plus `bitcoin-cli`. It has **no web UI** — its only
network surfaces are the JSON-RPC interface and the optional unauthenticated REST interface,
neither of which renders a page. Build system is CMake; the Qt GUI lives in a separate
repository and `BUILD_GUI` is off here.

## How the sandbox runs it

`docker-compose.base44.yml` (Base44's own file — do not replace it with the repo's build docs):

| service | what it does |
| --- | --- |
| `bitcoind` | configures + compiles `bitcoind`/`bitcoin-cli` **from the bind-mounted tree**, then execs the node in regtest. RPC reachable from the sandbox host on port **8000** (container 18443). |
| `dashboard` | the preview harness on port **3000** (see below). |

Non-obvious points:

- **The first boot compiles the node** (a few minutes on 4 cores). The dashboard is up
  immediately and shows a "starting up" state until RPC answers, so the preview is never blank.
  Compilation progress is visible in `docker compose -f docker-compose.base44.yml logs -f bitcoind`.
- The build directory lives in the `bitcoin-build` volume and ccache is enabled, so **restarts
  are incremental**. Editing C++ requires `docker compose -f docker-compose.base44.yml restart bitcoind`
  to rebuild + relaunch; edits never need an image rebuild (the image only holds the toolchain).
- The regtest chain lives in the `bitcoin-data` volume. It is a throwaway scratch chain —
  delete the volume to start again from genesis. Never delete `bitcoin-build` casually (full rebuild).
- `restart: on-failure` is set on both long-running services. One-shot setup services would be
  ordered with `depends_on: condition: service_completed_successfully` if any are added later.
- **No credentials are needed**: the dashboard authenticates to the node with the RPC cookie
  bitcoind writes into its datadir. Nothing here talks to an external service.

## The preview harness (`.base44/preview/`)

`server.py` (Python stdlib only, no dependencies) serves `index.html` on port 3000 and mirrors a
read-only slice of the node's RPC — chain height, peers, mempool, wallet balance, recent blocks —
plus a `POST /api/mine` that mines regtest blocks to the dashboard's own wallet. It is a
development aid, **not part of Bitcoin Core**; keep it confined to `.base44/`.

- `index.html` is read from disk on **every** request, so an edit shows after a browser refresh —
  there is no hot module reload here. Call `reload_preview` after changing it.
- Endpoints: `GET /` (page), `GET /api/status` (JSON snapshot; returns `{"ok": false, "state":
  "starting"}` while the node is still compiling), `POST /api/mine` `{"blocks": n}`.
- The proxy forwards the preview's external hostname, but the harness validates neither `Host`
  nor `Origin`, so no allowlist changes were required and no sandbox-only code paths exist
  (`BASE44_PREVIEW_MODE` is passed through but unused).

## Verifying it works

```bash
curl -s localhost:3000/api/status            # {"ok": true, "chain": "regtest", ...}
docker compose -f docker-compose.base44.yml exec -T bitcoind \
  /build/bin/bitcoin-cli -regtest -datadir=/data getblockchaininfo
docker compose -f docker-compose.base44.yml exec -T bitcoind \
  /build/bin/bitcoin-cli -regtest -datadir=/data -rpcwallet=base44preview getbalances
```

A healthy stack shows both containers `healthy` and the dashboard reporting `"ok": true`.

## Builds and tests outside the preview

The compose build deliberately trims the tree: `-DBUILD_GUI=OFF -DBUILD_TESTS=OFF -DBUILD_BENCH=OFF
-DBUILD_BITCOIN_BIN=OFF -DENABLE_IPC=OFF`. To build or run the unit tests, reconfigure the build
directory by hand (`cmake -B /build -S /src -DBUILD_TESTS=ON`, then `cmake --build /build`), or
compile on the host with the same apt packages as `.base44/Dockerfile.bitcoin`. The functional
tests in `test/functional/` need Python dependencies from `test/requirements.txt` and are not
installed in these images.
