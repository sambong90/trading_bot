#!/bin/bash
# 호스트(맥) 측 외장 SSD 마운트 감시. launchd가 2분마다 실행.
# /Volumes/OrbStackSSD 가 사라지면(드롭): 텔레그램 CRITICAL + OrbStack 정지 시도(쓰기 차단으로 손상 최소화).
# 외장이 DB 디스크라, 분리되면 VM/k8s가 곧 깨진다. 사전 차단은 불가하나 즉시 감지·정지로 피해 최소화.
set -uo pipefail

MOUNT="/Volumes/OrbStackSSD"
BACKUP_DIR="$HOME/db_backups"
ENV_FILE="$BACKUP_DIR/.telegram.env"
LOG="$BACKUP_DIR/ext_monitor.log"
STATE="$BACKUP_DIR/.ext_state"
ORB=/opt/homebrew/bin/orb
[ -x "$ORB" ] || ORB=$(command -v orb 2>/dev/null)

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
tg(){
  # shellcheck disable=SC1090
  [ -f "$ENV_FILE" ] && . "$ENV_FILE"
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] && \
    curl -s -m 15 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1
}

if mount | grep -q " ${MOUNT} "; then
  # 마운트 정상. 직전이 absent였으면 복구 알림.
  PREV=$(cat "$STATE" 2>/dev/null || echo unknown)
  echo present > "$STATE"
  if [ "$PREV" = "absent" ]; then
    echo "$(ts) external REMOUNTED" >> "$LOG"
    tg "🟢 [외장SSD] ${MOUNT} 다시 마운트됨. OrbStack 수동 기동 필요(외장 마운트 후 'orb start')."
  fi
  exit 0
else
  PREV=$(cat "$STATE" 2>/dev/null || echo unknown)
  echo absent > "$STATE"
  if [ "$PREV" != "absent" ]; then   # 상태 전이 시 1회만 알림/정지
    echo "$(ts) EXTERNAL DROPPED — DB disk missing" >> "$LOG"
    tg "🔴🔴 [외장SSD] ${MOUNT} 마운트 사라짐! DB 디스크 분리 위험 → OrbStack 정지 시도. 외장 재연결 후 'orb start' 필요."
    [ -n "$ORB" ] && "$ORB" stop >> "$LOG" 2>&1 || true
  fi
  exit 1
fi
