# CLAUDE.md — trading_bot 프로젝트 규칙

## 배포 규칙 (중요)

- **배포는 git push만 한다.** GitHub Actions가 빌드 → GHCR push → kubectl rollout restart까지 자동 처리.
- `kubectl rollout restart` 직접 실행 금지.
- `docker compose build` / `docker compose up` 직접 실행 금지.
- git push 후 GHA 완료(`gh run view`) 확인으로 배포 검증.

## 인프라 구성

- **런타임**: OrbStack Kubernetes (로컬 Mac Mini)
- **Namespace**: `quant-bot`
- **Deployment**: `trading-bot`
- **이미지**: `ghcr.io/sambong90/trading_bot:latest`
- **Self-hosted runner**: Mac Mini (GHA deploy job 실행)
- **이미지 pull 정책**: `Always` — restart 시 자동으로 최신 이미지 pull

## K8s Secrets 구조

| Secret 이름 | 방식 | 포함 키 |
|---|---|---|
| `trading-bot-secret` | 개별 env | UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, TELEGRAM_*, DB_URL |
| `trading-bot-secrets` | envFrom (통째로 주입) | COPILOT_ACCESS_TOKEN, FLASK_API_KEY |

- 새 환경변수 추가 시 `trading-bot-secrets`에 patch하면 자동으로 파드에 주입됨.

## AI Reviewer

- **API**: GitHub Copilot API (`https://api.githubcopilot.com`)
- **인증**: `COPILOT_ACCESS_TOKEN` (OAuth token, PAT 아님)
- **모델**: `gpt-4o`
- **스케줄**: 매주 일요일 KST 04:00 (auto_tuner 완료 후 순차 실행)
- **APScheduler timezone**: `Asia/Seoul` — `hour=4`는 KST 기준

## 봇 설정

- **모니터링 티커**: 60개 KRW 마켓
- **MAX_OPEN_POSITIONS**: 6
- **Circuit Breaker 임계값**: 일간 DD 5%
- **Panic Dip-Buy**: FNG ≤ 20 (Extreme Fear) + RSI ≤ 30 또는 BB하단 터치 시 발동
- **BUY_COOLDOWN_MINUTES**: 60분

## 주요 버그 수정 이력 (재발 방지)

1. **count_open_positions 오버카운트**: `_balance_cache` 전체 non-KRW를 카운트하여 MAX_OPEN_POSITIONS 가짜 도달 → 봇 관리 티커만 카운트하도록 수정 (2026-03-13)
2. **수동 매수 시 CB 오발동**: `compute_total_account_equity`가 수동 매수 자산 미포함 → 계좌 전체 자산 포함하도록 수정 (2026-03-13)
3. **매수 직후 CB 오발동**: Upbit 정산 딜레이로 `_balance_cache`에 매수 자산 미반영 → `_pending_buy_costs`로 equity 보정 (2026-03-15)
4. **CB 50% 매도 최소금액 에러**: `under_min_total_market_ask` → 매도 전 5000원 미만 체크 추가
