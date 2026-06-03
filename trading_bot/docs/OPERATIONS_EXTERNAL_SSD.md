# OPERATIONS — 외장 2TB SSD DB 운영 노트 (Phase 5)

DB가 OrbStack VM 디스크(data.img.raw)째로 외장 APFS SSD(/Volumes/OrbStackSSD)로 이전됨(2026-06-03).
외장이 단일 장애점이므로 아래 절차·자동화를 반드시 준수.

## 0. 구조 요약
- 봇/DB: OrbStack 단일노드 k8s(quant-bot). postgres-0 PV = local-path(VM 내부 /dev/vdb1).
- VM 디스크 실체: ~/Library/Group Containers/HUAQ24HBR6.dev.orbstack/data → (심볼릭 링크) → /Volumes/OrbStackSSD/orbstack_data/data.img.raw (sparse, 8TB 명목/실사용 15GB+).
- 내장 롤백본: 같은 위치의 data.bak (이전 직전 내장 원본). 수일 보존 후 정리.

## 1. 일일 백업 자동화 (완료)
- launchd: com.tradingbot.dbbackup (~/Library/LaunchAgents/com.tradingbot.dbbackup.plist)
- 스크립트: scripts/host/db_backup.sh (맥에서 실행, 컨테이너 아님)
- 동작: 매일 04:30 KST → kubectl exec postgres-0 pg_dump -Fc → ~/db_backups/daily/trading_bot_YYYYMMDD_HHMM.dump
- 로테이션: daily 최근 7개, 일요일은 weekly/ 복제(최근 4개 유지).
- 위치: 내장 ~/db_backups (외장 아님 — 3-2-1 원칙).
- 실패 시: 텔레그램 CRITICAL(🔴) 발송. 덤프 10MB 미만이면 비정상 처리·삭제.
- 클라우드 1부(선택, 미설정): rclone 설치 + ~/db_backups/.telegram.env 등에 RCLONE_REMOTE 설정 시 자동 복제. (현재 미구성 — 권장 보완 항목)
- 수동 실행/검증: `bash scripts/host/db_backup.sh` ; 로그 ~/db_backups/backup.log
- 인증: ~/db_backups/.telegram.env (TG_TOKEN/TG_CHAT, chmod 600, k8s secret에서 주입).

## 2. 외장 드롭 감지 → 봇 안전 정지 (완료)
- launchd: com.tradingbot.extmonitor (2분 간격, RunAtLoad)
- 스크립트: scripts/host/ext_ssd_monitor.sh
- 동작: /Volumes/OrbStackSSD 마운트 확인. 사라지면(드롭) 상태전이 1회 →
  텔레그램 CRITICAL(🔴🔴) + `orb stop`(추가 쓰기 차단으로 손상 최소화). 재마운트 시 복구 알림(🟢).
- 한계: 외장은 DB 디스크라 분리되면 VM/k8s가 즉시 깨진다. 사전 차단은 불가, 즉시 감지·정지로 피해 최소화가 목적.
  → 운영 중 절대 분리 금지(아래 4).
- 로그: ~/db_backups/ext_monitor.log, 상태 ~/db_backups/.ext_state
- 참고: 이 감시는 호스트(맥)에서 동작. 파드 내부 watchdog는 맥 /Volumes를 못 보므로 별도 호스트 감시가 필요.

## 3. 부팅 / 절전 순서 (반드시 준수)
- 재부팅 시:
  1) 외장 SSD가 /Volumes/OrbStackSSD로 자동 마운트됐는지 먼저 확인(APFS 자동마운트).
  2) 확인 후 OrbStack 수동 기동: `orb start` (또는 OrbStack 앱 실행).
  3) postgres-0/trading-bot 파드 Running 확인.
  - OrbStack 자동 기동은 OFF 유지(app.start_at_login=false). 외장 마운트 전에 기동되면 데이터 못 찾음.
- 절전: power.pause_in_sleep=true (맥 절전 시 VM 일시정지). 외장 연결만 유지되면 깨어날 때 안전.
  단, 장시간 절전 후엔 외장 마운트 상태 한 번 확인 권장.

## 4. FDA 권한 (핵심 — 없으면 부팅 실패)
- OrbStack은 외장(이동식 볼륨)의 data.img.raw에 락을 건다(lock_data_image 단계).
- macOS "전체 디스크 접근(Full Disk Access)" / "이동식 볼륨의 파일" 권한이 OrbStack에 부여돼 있어야 한다.
  없으면 권한 프롬프트가 락을 블록 → lock_data_image 타임아웃 → 부팅 실패(이번 이전 1차 실패 원인).
- 확인: 시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한 → OrbStack 켜짐.
- 이 권한은 재부팅/업데이트 후에도 유지되나, OrbStack 업데이트 시 재확인 권장.

## 5. 물리 안정성
- 외장(UnionSine USB 3.2 Gen2, 10Gbps, 실측 ~1GB/s)은 Mac mini 본체 포트에 직결됨(서드파티 USB 허브 경유 아님 — 유지).
- 운영 중 절대 분리 금지(분리=DB 디스크 분리=손상). 케이블/발열/전원 안정 유지.
- 인클로저 교체 시 OrbStack 정지 후 진행.

## 6. data.bak 정리 시점
- 내장 ~/Library/Group Containers/HUAQ24HBR6.dev.orbstack/data.bak (이전 직전 내장 원본, ~15GB).
- 보존 기간: 외장 운영이 수일(권장 3~5일) 무사고로 확인될 때까지.
- 정리: 안정 확인 후 `rm -rf "data.bak"`. 그 전엔 절대 삭제 금지(롤백 안전망).
- 결혼식 원본 백업: ~/external_rescue/ (외장 재포맷으로 외장에선 삭제됨, 내장에만 존재 — 별도 보관처 결정 필요).

## 7. 롤백 절차 (외장 문제 시 내장 복귀)
1) `orb stop`
2) `rm "$HOME/Library/Group Containers/HUAQ24HBR6.dev.orbstack/data"` (심링크 제거)
3) `mv "$HOME/Library/.../data.bak" "$HOME/Library/.../data"`
4) `orb start` → 내장에서 기동. (data.bak 보존 기간 내에서만 가능)

## 8. 백업 복구 절차
- 임시 컨테이너 검증: `docker run -d --name pgr -e POSTGRES_PASSWORD=x postgres:16`
  → `docker cp <dump> pgr:/tmp/d.dump` → `docker exec pgr createdb -U postgres t`
  → `docker exec pgr pg_restore -U postgres -d t --no-owner /tmp/d.dump` → row 수 검증.
- 운영 복구: 새 빈 DB에 `pg_restore -d trading_bot --clean --if-exists <dump>` (서비스 정지 후).
