"""Secretless image qualification; never starts a server, indexer, or model."""

from __future__ import annotations

import asyncio
import importlib.metadata as metadata
import json
import subprocess
import sys


def command(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=20).stdout.strip()


def main() -> None:
    assert sys.version_info[:2] == (3, 14), sys.version
    command([sys.executable, "-m", "pip", "check"])
    node = command(["node", "--version"])
    npm = command(["npm", "--version"])
    assert node.startswith("v24."), node
    assert npm == "12.0.2", npm
    # Loading the installed native parser checks Node/ABI compatibility.
    # No CLI command, database, repository, model download, or index is opened.
    native = json.loads(command(["node", "-e", """
const {createRequire} = require('node:module');
const fs = require('node:fs');
const {dirname, resolve} = require('node:path');
const path = '/usr/local/lib/node_modules/gitnexus/package.json';
const req = createRequire(path);
const pkg = req(path);
const declaredBin = resolve(dirname(path), pkg.bin.gitnexus);
if (fs.realpathSync('/usr/local/bin/gitnexus') !== declaredBin) throw new Error('CLI launcher mismatch');
fs.accessSync(declaredBin, fs.constants.X_OK);
const Parser = req('tree-sitter');
const parser = new Parser();
if (typeof parser.parse !== 'function') throw new Error('native parser unavailable');
console.log(JSON.stringify({gitnexus: pkg.version, treeSitter: 'loaded', cliBin: pkg.bin.gitnexus, cliExecutable: true}));
"""]))
    assert native["gitnexus"] == "1.6.11", native

    import server

    versions = {name: metadata.version(name) for name in
                ("mcp", "starlette", "uvicorn", "python-dotenv")}
    assert versions["mcp"].startswith("1."), versions
    public_health = json.loads(asyncio.run(server.health_check(None)).body)
    assert public_health == {"status": "ok", "service": "hlt-codegraph"}
    assert server.auth_failure("/health", "", None) is None
    for path in ("/", "/mcp", "/readiness", "/verify-source"):
        assert server.auth_failure(path, "", None)[0] == 503
        assert server.auth_failure(path, "Bearer invalid", "offline-fixture")[0] == 401
        assert server.auth_failure(path, "Bearer offline-fixture", "offline-fixture") is None
    print(json.dumps({"status": "PASS", "python": sys.version.split()[0],
                      "node": node, "npm": npm, **native, "pythonPackages": versions,
                      "health": public_health, "authPaths": 4,
                      "serverStarted": False, "indexingStarted": False}, sort_keys=True))


if __name__ == "__main__":
    main()
