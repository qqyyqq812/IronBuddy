#!/usr/bin/env bash
set -euo pipefail

echo "== time =="
date '+%Y-%m-%d %H:%M:%S %Z'

echo
echo "== workspace =="
pwd

echo
echo "== WSL resources =="
free -h
echo
df -h / /mnt/c /mnt/d /mnt/e 2>/dev/null || true

echo
echo "== inotify limits =="
printf 'max_user_watches='
cat /proc/sys/fs/inotify/max_user_watches
printf 'max_user_instances='
cat /proc/sys/fs/inotify/max_user_instances

echo
echo "== Antigravity WSL processes =="
ps -eo pid,ppid,comm,%mem,%cpu,rss,args --sort=-rss |
  awk '
    NR == 1 { print; next }
    /antigravity-server|language_server_linux|openai.chatgpt|claude-code|latex-workshop|fileWatcher|extensionHost/ {
      line = $0
      if (length(line) > 220) {
        line = substr(line, 1, 220) " ..."
      }
      print line
    }
  ' || true

echo
echo "== largest Antigravity state directories =="
du -sh "$HOME/.antigravity-server" "$HOME/.gemini/antigravity" 2>/dev/null || true
du -sh "$HOME/.antigravity-server"/bin/* 2>/dev/null | sort -h | tail -12 || true
du -sh "$HOME/.gemini/antigravity"/* 2>/dev/null | sort -h | tail -12 || true

echo
echo "== current repo heavy directories =="
du -xh --max-depth=1 . 2>/dev/null | sort -h | tail -20 || true

echo
echo "== latest Antigravity log errors =="
latest_log="$(ls -1dt "$HOME/.antigravity-server"/data/logs/* 2>/dev/null | head -1 || true)"
if [[ -n "${latest_log}" ]]; then
  echo "latest_log=${latest_log}"
  rg -n -i 'oom|out of memory|enospc|inotify|watcher|fatal|crash|killed|terminated|signal: 6|extension host|error' \
    "${latest_log}" --glob '*.log' 2>/dev/null |
    awk '{ if (length($0) > 260) print substr($0, 1, 260) " ..."; else print }' |
    head -120 || true
else
  echo "No Antigravity logs found."
fi

echo
echo "== recent kernel crash/OOM lines =="
dmesg -T 2>/dev/null | rg -i 'CaptureCrash|signal: 6|out of memory|oom|killed process|antigravity|node' | tail -80 || true
