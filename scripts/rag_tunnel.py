#!/usr/bin/env python3
"""Persistent SSH tunnel for cloud vector RAG services.

Python fallback for boards where expect is absent or unreliable.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", ".api_config.json")

with open(CONFIG, "r") as f:
    cfg = json.load(f)

HOST = cfg.get("CLOUD_SSH_HOST", "")
PORT = cfg.get("CLOUD_SSH_PORT", 22)
USER = cfg.get("CLOUD_SSH_USER", "root")
PASS = cfg.get("CLOUD_SSH_PASSWORD", "")
VECTOR_LPORT = cfg.get("RAG_VECTOR_LOCAL_PORT", 6333)
EMBED_LPORT = cfg.get("RAG_EMBEDDING_LOCAL_PORT", 8008)

if not HOST or not PASS:
    print("[rag_tunnel.py] missing credentials", file=sys.stderr)
    sys.exit(1)

try:
    import pexpect
except ImportError:
    print("[rag_tunnel.py] pexpect not installed", file=sys.stderr)
    sys.exit(1)

cmd = (
    "ssh -N -L {vp}:127.0.0.1:6333 -L {ep}:127.0.0.1:8008 -p {p} "
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o ControlMaster=no -o ControlPath=none "
    "-o ServerAliveInterval=30 -o ServerAliveCountMax=3 "
    "-o ExitOnForwardFailure=yes {u}@{h}"
).format(vp=VECTOR_LPORT, ep=EMBED_LPORT, p=PORT, u=USER, h=HOST)

while True:
    print("[rag_tunnel.py] spawning tunnel", flush=True)
    child = pexpect.spawn(cmd, encoding="utf-8", timeout=25)
    try:
        i = child.expect(["[Pp]assword:", "yes/no", pexpect.EOF, pexpect.TIMEOUT],
                         timeout=15)
        if i == 1:
            child.sendline("yes")
            child.expect("[Pp]assword:", timeout=10)
            child.sendline(PASS)
        elif i == 0:
            child.sendline(PASS)
        elif i == 3:
            # Public-key auth may establish the -N tunnel without any password
            # prompt. If ssh stays alive past the startup window, hold it.
            print("[rag_tunnel.py] no password prompt; assuming tunnel established", flush=True)
        else:
            print("[rag_tunnel.py] startup did not reach password prompt", flush=True)
            time.sleep(5)
            continue
        print("[rag_tunnel.py] tunnel established, holding", flush=True)
        child.expect(pexpect.EOF, timeout=None)
    except Exception as exc:
        print("[rag_tunnel.py] exception: %s" % exc, flush=True)
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    print("[rag_tunnel.py] tunnel dropped, reconnecting in 5s", flush=True)
    time.sleep(5)
