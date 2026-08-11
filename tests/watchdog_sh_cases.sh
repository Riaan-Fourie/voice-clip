#!/bin/bash
# Exercise watchdog.sh's branches in a sandboxed HOME with osascript stubbed out,
# so a "relaunch" is observable but never actually opens a Terminal window.
set -uo pipefail

WD="$1"
SANDBOX=$(mktemp -d)
export HOME="$SANDBOX"
mkdir -p "$HOME/.voice-clip" "$SANDBOX/bin"

# Stub osascript: record the relaunch instead of performing it.
cat > "$SANDBOX/bin/osascript" <<'EOF'
#!/bin/bash
cat > /dev/null
echo "RELAUNCH_CALLED" >> "$HOME/.voice-clip/relaunch.calls"
EOF
chmod +x "$SANDBOX/bin/osascript"
export PATH="$SANDBOX/bin:$PATH"

STATE="$HOME/.voice-clip"
pass=0; fail=0
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "  ok   — $1"; pass=$((pass+1));
  else echo "  FAIL — $1 (expected '$2', got '$3')"; fail=$((fail+1)); fi
}
relaunches() { [ -f "$STATE/relaunch.calls" ] && wc -l < "$STATE/relaunch.calls" | tr -d ' ' || echo 0; }
reset_state() { rm -f "$STATE"/{relaunch.calls,watchdog.laststart,heartbeat,voiceclip.pid,watchdog.log}; }

# A process whose command line looks like the real app, so running_pid() matches.
# stdout/stderr MUST be redirected: a background child inheriting the command
# substitution's pipe keeps it open, so `pid=$(fake_app)` would block until the
# sleep finished rather than returning immediately.
fake_app() { bash -c 'exec -a "python main.py" sleep 120' >/dev/null 2>&1 & echo $!; }

echo "== 1. no pidfile → relaunch =="
reset_state
bash "$WD" >/dev/null 2>&1
check "relaunched" 1 "$(relaunches)"

echo "== 2. alive + FRESH heartbeat → do nothing =="
reset_state
pid=$(fake_app); echo "$pid" > "$STATE/voiceclip.pid"
echo "$(date +%s) $pid" > "$STATE/heartbeat"
bash "$WD" >/dev/null 2>&1
check "no relaunch" 0 "$(relaunches)"
check "process untouched" "alive" "$(ps -p "$pid" >/dev/null 2>&1 && echo alive || echo dead)"
kill "$pid" 2>/dev/null

echo "== 3. alive + STALE heartbeat → kill -9 and relaunch (the #333 case) =="
reset_state
pid=$(fake_app); echo "$pid" > "$STATE/voiceclip.pid"
echo "$(( $(date +%s) - 600 )) $pid" > "$STATE/heartbeat"
bash "$WD" >/dev/null 2>&1
check "relaunched" 1 "$(relaunches)"
check "wedged process killed" "dead" "$(ps -p "$pid" >/dev/null 2>&1 && echo alive || echo dead)"
check "logged the wedge" "yes" "$(grep -q 'ALIVE BUT WEDGED' "$STATE/watchdog.log" && echo yes || echo no)"
kill "$pid" 2>/dev/null

echo "== 4. alive + stale heartbeat from a DIFFERENT pid → leave it alone =="
reset_state
pid=$(fake_app); echo "$pid" > "$STATE/voiceclip.pid"
echo "$(( $(date +%s) - 600 )) 999999" > "$STATE/heartbeat"   # leftover from a prior run
bash "$WD" >/dev/null 2>&1
check "no relaunch" 0 "$(relaunches)"
check "healthy process survived" "alive" "$(ps -p "$pid" >/dev/null 2>&1 && echo alive || echo dead)"
kill "$pid" 2>/dev/null

echo "== 5. alive + NO heartbeat file (pre-#333 build) → leave it alone =="
reset_state
pid=$(fake_app); echo "$pid" > "$STATE/voiceclip.pid"
bash "$WD" >/dev/null 2>&1
check "no relaunch" 0 "$(relaunches)"
check "process survived" "alive" "$(ps -p "$pid" >/dev/null 2>&1 && echo alive || echo dead)"
kill "$pid" 2>/dev/null

echo "== 6. stale heartbeat but inside relaunch cooldown → don't kill without relaunching =="
reset_state
pid=$(fake_app); echo "$pid" > "$STATE/voiceclip.pid"
echo "$(( $(date +%s) - 600 )) $pid" > "$STATE/heartbeat"
touch "$STATE/watchdog.laststart"   # just relaunched → in cooldown
bash "$WD" >/dev/null 2>&1
check "no relaunch" 0 "$(relaunches)"
check "process NOT orphan-killed" "alive" "$(ps -p "$pid" >/dev/null 2>&1 && echo alive || echo dead)"
kill "$pid" 2>/dev/null

echo
echo "passed=$pass failed=$fail"
rm -rf "$SANDBOX"
[ "$fail" -eq 0 ]
