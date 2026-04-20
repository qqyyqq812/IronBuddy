#!/usr/bin/env python3
"""Python fallback for SSH password automation. Used only if `expect` not available."""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", ".api_config.json")

with open(CONFIG, "r") as f:
    cfg = json.load(f)

HOST = cfg.get("CLOUD_SSH_HOST", "")
PORT = cfg.get("CLOUD_SSH_PORT", 22)
USER = cfg.get("CLOUD_SSH_USER", "root")
PASS = cfg.get("CLOUD_SSH_PASSWORD", "")
LPORT = cfg.get("CLOUD_LOCAL_TUNNEL_PORT", 6006)

if not HOST or not PASS:
    print("[cloud_tunnel.py] missing credentials", file=sys.stderr)
    sys.exit(1)

try:
    import pexpect
except ImportError:
    print("[cloud_tunnel.py] pexpect not installed", file=sys.stderr)
    sys.exit(1)

cmd = (
    "ssh -N -L {lp}:127.0.0.1:6006 -p {p} "
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o ServerAliveInterval=30 -o ServerAliveCountMax=3 "
    "-o ExitOnForwardFailure=yes {u}@{h}"
).format(lp=LPORT, p=PORT, u=USER, h=HOST)

while True:
    print("[cloud_tunnel.py] spawning:", cmd, flush=True)
    c = pexpect.spawn(cmd, encoding="utf-8", timeout=25)
    try:
        i = c.expect(["[Pp]assword:", "yes/no", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
        if i == 1:
            c.sendline("yes")
            c.expect("[Pp]assword:", timeout=10)
            c.sendline(PASS)
        elif i == 0:
            c.sendline(PASS)
        else:
            print("[cloud_tunnel.py] unexpected startup state, will retry", flush=True)
            time.sleep(5)
            continue
        print("[cloud_tunnel.py] tunnel established, holding...", flush=True)
        c.expect(pexpect.EOF, timeout=None)  # block until tunnel dies
    except Exception as e:
        print("[cloud_tunnel.py] exception:", e, flush=True)
    finally:
        try: c.close(force=True)
        except Exception: pass
    print("[cloud_tunnel.py] tunnel dropped, reconnecting in 5s...", flush=True)
    time.sleep(5)
