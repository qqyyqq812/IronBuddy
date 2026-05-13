#!/usr/bin/env bash
set -euo pipefail

apply=0
if [[ "${1:-}" == "--apply" ]]; then
  apply=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--apply]" >&2
  exit 2
fi

server_bin="$HOME/.antigravity-server/bin"
recordings="$HOME/.gemini/antigravity/browser_recordings"

echo "== Antigravity WSL cleanup =="
if [[ "${apply}" -eq 0 ]]; then
  echo "Mode: dry-run. Re-run with --apply to remove the listed disposable items."
else
  echo "Mode: apply."
fi

echo
echo "== running Antigravity server versions =="
mapfile -t running_versions < <(
  python3 - <<'PY'
import os
import pathlib

root = pathlib.Path.home() / ".antigravity-server" / "bin"
versions = set()
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        raw = pathlib.Path("/proc") / pid / "cmdline"
        cmdline = raw.read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
    except OSError:
        continue
    marker = str(root) + "/"
    if marker not in cmdline:
        continue
    after = cmdline.split(marker, 1)[1]
    version = after.split("/", 1)[0].split(" ", 1)[0]
    if version and (root / version).is_dir():
        versions.add(version)
for version in sorted(versions):
    print(version)
PY
)
if [[ "${#running_versions[@]}" -eq 0 ]]; then
  echo "No running Antigravity server version detected."
else
  printf '%s\n' "${running_versions[@]}"
fi

echo
echo "== old server versions selected for removal =="
if [[ -d "${server_bin}" ]]; then
  while IFS= read -r dir; do
    name="$(basename "${dir}")"
    keep=0
    for running in "${running_versions[@]}"; do
      if [[ "${name}" == "${running}" ]]; then
        keep=1
        break
      fi
    done
    if [[ "${keep}" -eq 1 ]]; then
      echo "KEEP running ${dir}"
      continue
    fi
    size="$(du -sh "${dir}" 2>/dev/null | awk '{print $1}')"
    echo "REMOVE ${size:-?} ${dir}"
    if [[ "${apply}" -eq 1 ]]; then
      rm -rf -- "${dir}"
    fi
  done < <(find "${server_bin}" -mindepth 1 -maxdepth 1 -type d | sort)
else
  echo "No ${server_bin} directory found."
fi

echo
echo "== old browser recordings selected for removal =="
if [[ -d "${recordings}" ]]; then
  while IFS= read -r dir; do
    size="$(du -sh "${dir}" 2>/dev/null | awk '{print $1}')"
    mtime="$(stat -c '%y' "${dir}" 2>/dev/null | cut -d'.' -f1)"
    echo "REMOVE ${size:-?} ${mtime:-?} ${dir}"
    if [[ "${apply}" -eq 1 ]]; then
      rm -rf -- "${dir}"
    fi
  done < <(find "${recordings}" -mindepth 1 -maxdepth 1 -type d -mtime +14 | sort)
else
  echo "No ${recordings} directory found."
fi

echo
echo "== remaining sizes =="
du -sh "$HOME/.antigravity-server" "$HOME/.gemini/antigravity" 2>/dev/null || true
