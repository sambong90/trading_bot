#!/bin/bash
# 호스트(맥) 측 일일 DB 백업. launchd가 1일 1회 실행.
# pg_dump -Fc -> ~/db_backups/daily, 일요일엔 weekly 복제. 로테이션(일7/주4).
# 실패 시 텔레그램 CRITICAL. 백업은 내장(외장 아님). 클라우드는 rclone 설정 시 1부 추가.
set -uo pipefail

BACKUP_DIR="$HOME/db_backups"
DAILY_DIR="$BACKUP_DIR/daily"
WEEKLY_DIR="$BACKUP_DIR/weekly"
LOG="$BACKUP_DIR/backup.log"
ENV_FILE="$BACKUP_DIR/.telegram.env"
NS=quant-bot; POD=postgres-0; DB=trading_bot; DBUSER=botuser
MIN_SIZE=10000000   # 10MB 미만이면 비정상으로 간주

# launchd는 PATH가 최소라 절대경로 사용
KCTL=/opt/homebrew/bin/kubectl
[ -x "$KCTL" ] || KCTL=$(command -v kubectl 2>/dev/null)

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "$(ts) $*" >> "$LOG"; }
tg(){
  # shellcheck disable=SC1090
  [ -f "$ENV_FILE" ] && . "$ENV_FILE"
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] && \
    curl -s -m 15 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1
}

STAMP=$(date +%Y%m%d_%H%M)
OUT="$DAILY_DIR/trading_bot_${STAMP}.dump"
log "backup start -> $OUT"

if [ -z "$KCTL" ]; then
  log "FAIL: kubectl not found"; tg "🔴 [DB백업] kubectl 없음 — 백업 불가 $STAMP"; exit 1
fi

if "$KCTL" -n "$NS" exec "$POD" -- pg_dump -U "$DBUSER" -Fc -d "$DB" > "$OUT" 2>>"$LOG"; then
  SZ=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
  if [ "$SZ" -lt "$MIN_SIZE" ]; then
    log "FAIL: dump too small ($SZ bytes)"; tg "🔴 [DB백업] 덤프 비정상(작음 ${SZ}B) $STAMP"; rm -f "$OUT"; exit 1
  fi
  log "backup OK size=$SZ"
  # 일요일(=7)이면 주간 보관 복제
  if [ "$(date +%u)" = "7" ]; then cp "$OUT" "$WEEKLY_DIR/" && log "weekly copy"; fi
  # 로테이션: daily 최근 7개, weekly 최근 4개 유지
  ls -1t "$DAILY_DIR"/trading_bot_*.dump 2>/dev/null | tail -n +8 | while read -r f; do rm -f "$f"; done
  ls -1t "$WEEKLY_DIR"/trading_bot_*.dump 2>/dev/null | tail -n +5 | while read -r f; do rm -f "$f"; done
  # 클라우드 1부(선택): RCLONE_REMOTE 가 .telegram.env 등에 설정돼 있으면 복제
  if command -v rclone >/dev/null 2>&1 && [ -n "${RCLONE_REMOTE:-}" ]; then
    if rclone copy "$OUT" "$RCLONE_REMOTE" >>"$LOG" 2>&1; then log "cloud copy ok"; else log "cloud copy fail"; tg "⚠️ [DB백업] 클라우드 복제 실패 $STAMP"; fi
  fi
  # 연구 워크스테이션(데스크톱) LAN scp push — 최신 덤프 1부 전송.
  # 데스크톱 off면 실패 무시(로컬백업 보존, 다음 기동 때 따라잡음). 3일 연속 실패 시 텔레그램.
  # ~/.ssh/config 의 'desktop' 별칭(192.168.123.111:22, id_ed25519, accept-new) 사용. LAN 한정.
  PUSH_TARGET="desktop:db_sync/incoming/"
  FAILCNT_FILE="$BACKUP_DIR/.push_fail_count"
  if /usr/bin/scp -p -o BatchMode=yes -o ConnectTimeout=15 "$OUT" "$PUSH_TARGET" >>"$LOG" 2>&1; then
    log "push OK -> $PUSH_TARGET $(basename "$OUT")"
    echo 0 > "$FAILCNT_FILE"
  else
    FAILS=$(( $(cat "$FAILCNT_FILE" 2>/dev/null || echo 0) + 1 ))
    echo "$FAILS" > "$FAILCNT_FILE"
    log "push FAIL (${FAILS}연속) -> $PUSH_TARGET (데스크톱 off 가능, 로컬백업 보존)"
    [ "$FAILS" -ge 3 ] && tg "⚠️ [DB백업] 데스크톱 전송 ${FAILS}일 연속 실패 — 연구WS 동기화 점검 $STAMP"
  fi
  log "backup done ($SZ bytes)"
else
  log "FAIL: pg_dump error (OrbStack/postgres 확인)"; tg "🔴 [DB백업] pg_dump 실패 $STAMP — OrbStack/postgres 확인"; rm -f "$OUT"; exit 1
fi
