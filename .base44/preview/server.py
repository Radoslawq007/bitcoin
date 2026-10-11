#!/usr/bin/env python3
"""Base44 dev dashboard for Bitcoin Core.

Bitcoin Core itself is a headless daemon (`bitcoind`) whose only network
interfaces are JSON-RPC and the unauthenticated REST interface -- neither
renders a page. This tiny stdlib-only server is the preview harness: it serves
`index.html` on port 3000 and proxies a handful of read-only RPC calls so the
running node can be seen in the Base44 preview.

It is a development aid, not part of Bitcoin Core. It holds no credentials of
its own: it authenticates to the node with the RPC cookie that `bitcoind`
writes into its data directory.
"""

import base64
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RPC_HOST = os.environ.get("BITCOIN_RPC_HOST", "127.0.0.1")
RPC_PORT = int(os.environ.get("BITCOIN_RPC_PORT", "18443"))
DATA_DIR = os.environ.get("BITCOIN_DATADIR", "/data")
NETWORK = os.environ.get("BITCOIN_NETWORK", "regtest")
WALLET = os.environ.get("BITCOIN_WALLET", "base44preview")
PORT = int(os.environ.get("PORT", "3000"))
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

RPC_URL = "http://%s:%d/" % (RPC_HOST, RPC_PORT)


class RpcUnavailable(Exception):
    """The node is not answering RPC yet (still compiling, or booting)."""


class RpcError(Exception):
    """The node answered with a JSON-RPC error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_cookie():
    try:
        with open(os.path.join(DATA_DIR, NETWORK, ".cookie"), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def rpc(method, params=None, wallet=None, timeout=25):
    cookie = _rpc_cookie()
    if not cookie:
        raise RpcUnavailable("waiting for the node's RPC cookie")
    url = RPC_URL + ("wallet/%s" % urllib.parse.quote(wallet) if wallet else "")
    payload = json.dumps(
        {"jsonrpc": "1.0", "id": "base44-preview", "method": method, "params": params or []}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(cookie.encode("utf-8")).decode("ascii"),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            raise RpcUnavailable("RPC HTTP %s" % exc.code)
        error = body.get("error") or {}
        raise RpcError(error.get("code"), error.get("message") or "HTTP %s" % exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RpcUnavailable(str(exc))
    if body.get("error"):
        raise RpcError(body["error"].get("code"), body["error"].get("message"))
    return body.get("result")


def _recent_blocks(tip_hash, tip_height, limit=6):
    blocks = []
    block_hash = tip_hash
    while block_hash and len(blocks) < limit:
        header = rpc("getblockheader", [block_hash])
        blocks.append(
            {
                "hash": header["hash"],
                "height": header["height"],
                "time": header["time"],
                "txs": header.get("nTx", 0),
                "size": None,
                "confirmations": tip_height - header["height"] + 1,
            }
        )
        block_hash = header.get("previousblockhash")
    return blocks


def _wallet_state():
    try:
        balances = rpc("getbalances", wallet=WALLET)
    except (RpcError, RpcUnavailable):
        return None
    mine = balances.get("mine", {})
    return {
        "name": WALLET,
        "trusted": mine.get("trusted", 0),
        "immature": mine.get("immature", 0),
        "untrusted_pending": mine.get("untrusted_pending", 0),
    }


def status_payload():
    chain = rpc("getblockchaininfo")
    network = rpc("getnetworkinfo")
    mempool = rpc("getmempoolinfo")
    tip_height = chain["blocks"]
    return {
        "ok": True,
        "chain": chain["chain"],
        "version": network["subversion"],
        "protocol_version": network["protocolversion"],
        "connections": network["connections"],
        "blocks": tip_height,
        "headers": chain["headers"],
        "best_block_hash": chain["bestblockhash"],
        "difficulty": chain["difficulty"],
        "verification_progress": chain["verificationprogress"],
        "size_on_disk": chain["size_on_disk"],
        "pruned": chain["pruned"],
        "mempool_txs": mempool["size"],
        "mempool_bytes": mempool["bytes"],
        "recent_blocks": _recent_blocks(chain["bestblockhash"], tip_height),
        "wallet": _wallet_state(),
    }


def ensure_wallet():
    try:
        rpc("getwalletinfo", wallet=WALLET)
        return
    except RpcError as exc:
        if exc.code not in (-18, -19):
            raise
    try:
        rpc("createwallet", {"wallet_name": WALLET, "load_on_startup": True})
    except RpcError as exc:
        if exc.code != -4:  # -4: wallet already exists on disk, merely not loaded
            raise
        rpc("loadwallet", {"filename": WALLET, "load_on_startup": True})


def mine_blocks(count):
    ensure_wallet()
    address = rpc(
        "getnewaddress", {"label": "base44-preview-mining"}, wallet=WALLET
    )
    hashes = rpc("generatetoaddress", [count, address])
    return {"mined": len(hashes), "blocks": hashes, "address": address}


class Handler(BaseHTTPRequestHandler):
    server_version = "Base44BitcoinPreview/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the container log readable
        if not self.path.startswith("/api/status"):
            super().log_message(fmt, *args)

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code, payload):
        self._send(code, json.dumps(payload), "application/json; charset=utf-8")

    def do_GET(self):
        path = posixpath.normpath(urllib.parse.urlparse(self.path).path)
        if path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as handle:
                    page = handle.read()
            except OSError:
                self._send(500, "index.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            try:
                self._send_json(200, status_payload())
            except RpcUnavailable as exc:
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "state": "starting",
                        "message": "The node is not serving RPC yet: %s" % exc,
                    },
                )
            except RpcError as exc:
                self._send_json(200, {"ok": False, "state": "error", "message": exc.message})
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/mine":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            self._send_json(400, {"ok": False, "message": "invalid JSON body"})
            return
        try:
            count = int(payload.get("blocks", 1))
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 100))
        try:
            result = mine_blocks(count)
            result["ok"] = True
            self._send_json(200, result)
        except RpcUnavailable as exc:
            self._send_json(200, {"ok": False, "state": "starting", "message": str(exc)})
        except RpcError as exc:
            self._send_json(200, {"ok": False, "state": "error", "message": exc.message})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("Base44 preview dashboard on http://0.0.0.0:%d" % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
