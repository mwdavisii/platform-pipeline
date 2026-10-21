import json
import shutil
import subprocess
from pathlib import Path

import pytest

from builder.config import load_platform
from builder.render.authz import render_authz

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

TABLE = json.loads((Path(__file__).parent / "authz_cases.json").read_text(encoding="utf-8"))
CASES = [c[:4] for c in TABLE["cases"] if "node" not in (c[4] if len(c) > 4 else [])]

# Sends each raw request over a plain socket (so we control the exact bytes) to a real server whose
# listeners accept everything, and prints the last status line of each response.
PROBE = r"""
const http = require('node:http');
const net = require('node:net');
const ok = (res) => { res.writeHead(200); res.end('ok'); };
const server = http.createServer((req, res) => ok(res));
server.on('checkContinue', (req, res) => ok(res));
server.on('checkExpectation', (req, res) => ok(res));
const reply = (req, socket) => socket.end('HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
server.on('upgrade', reply);
server.on('connect', reply);
server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  const out = [];
  for (const raw of JSON.parse(process.argv[2])) {
    out.push(await new Promise((resolve) => {
      let data = '';
      const s = net.connect(port, '127.0.0.1', () => s.write(raw));
      s.setTimeout(3000, () => s.destroy());
      s.on('data', (d) => { data += d; });
      s.on('error', () => {});
      s.on('close', () => {
        const all = [...data.matchAll(/HTTP\/1\.1 (\d{3})/g)];
        const last = all.length ? data.slice(all[all.length - 1].index) : '';
        resolve({ status: all.length ? Number(all[all.length - 1][1]) : 0, marked: /^x-platform-authz: deny\r$/im.test(last) });
      });
    }));
  }
  console.log(JSON.stringify(out));
  server.close();
});
"""


@pytest.fixture
def guard_dir(tmp_path, node_spec):
    node_spec["authz"] = TABLE["authz"]
    files = render_authz(node_spec, load_platform())
    assert sorted(files) == ["_platform/authz-policy.json", "_platform/authz.js"]
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "probe.js").write_text(PROBE, encoding="utf-8")
    return tmp_path


def raw_request(method, path, roles=None, extra=(), body=False):
    lines = [f"{method} {path} HTTP/1.1", "Host: x", "Connection: close", *extra]
    if roles is not None:
        lines.append(f"X-Platform-Roles: {roles}")
    if body:
        lines.append("Content-Length: 0")
    return "\r\n".join(lines) + "\r\n\r\n"


def probe(directory, raws):
    return [r["status"] for r in probe_full(directory, raws)]


def probe_full(directory, raws):
    out = subprocess.run(
        ["node", "--require", "./_platform/authz.js", "probe.js", json.dumps(raws)],
        cwd=directory, capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(out.stdout)


def test_node_guard_enforces_shared_table(guard_dir):
    raws = [raw_request(m, p, r, body=m in ("POST", "PUT", "PATCH", "DELETE")) for m, p, r, _ in CASES]
    results = probe_full(guard_dir, raws)
    assert [r["status"] for r in results] == [c[3] for c in CASES]
    assert [r["marked"] for r in results] == [c[3] == 403 for c in CASES]


def test_socket_denials_carry_the_marker(guard_dir):
    upgrade = ["Connection: Upgrade", "Upgrade: websocket"]
    results = probe_full(guard_dir, [raw_request("CONNECT", "/api/notes", "writer"), raw_request("GET", "/api/notes", None, upgrade),
                                     raw_request("GET", "/api/notes", "reader", upgrade)])
    assert [(r["status"], r["marked"]) for r in results] == [(403, True), (403, True), (200, False)]


def test_duplicate_identity_headers_mean_no_roles(guard_dir):
    dup = raw_request("POST", "/api/notes", "reader", ["X-Platform-Roles: writer"], body=True)
    same = raw_request("GET", "/api/notes", "reader", ["X-Platform-Roles: reader"])
    single = raw_request("GET", "/api/notes", "reader")
    health = raw_request("GET", "/healthz", "reader", ["X-Platform-Roles: reader"])
    assert probe(guard_dir, [dup, same, single, health]) == [403, 403, 200, 200]


def test_expect_continue_is_guarded(guard_dir):
    expect = ["Expect: 100-continue"]
    denied = raw_request("POST", "/api/notes", "reader", expect, body=True)
    allowed = raw_request("POST", "/api/notes", "writer", expect, body=True)
    assert probe(guard_dir, [denied, allowed]) == [403, 200]


def test_expect_expectation_is_guarded(guard_dir):
    custom = ["Expect: something-else"]
    denied = raw_request("POST", "/api/notes", "reader", custom, body=True)
    allowed = raw_request("POST", "/api/notes", "writer", custom, body=True)
    assert probe(guard_dir, [denied, allowed]) == [403, 200]


def test_connect_is_denied(guard_dir):
    assert probe(guard_dir, [raw_request("CONNECT", "example.com:443", "writer"), raw_request("CONNECT", "/api/notes", "writer")]) == [403, 403]


def test_upgrade_is_guarded_by_its_real_method(guard_dir):
    upgrade = ["Connection: Upgrade", "Upgrade: websocket"]
    post = raw_request("POST", "/api/notes", "reader", upgrade, body=True)
    anonymous = raw_request("GET", "/api/notes", None, upgrade)
    health = raw_request("GET", "/healthz", None, upgrade)
    reader = raw_request("GET", "/api/notes", "reader", upgrade)
    assert probe(guard_dir, [post, anonymous, health, reader]) == [403, 403, 403, 200]


def test_http2_servers_are_refused(guard_dir):
    for factory in ("createServer", "createSecureServer"):
        result = subprocess.run(
            ["node", "--require", "./_platform/authz.js", "-e", f"require('node:http2').{factory}()"],
            cwd=guard_dir, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "http2 servers are not supported under platform authz" in result.stderr
