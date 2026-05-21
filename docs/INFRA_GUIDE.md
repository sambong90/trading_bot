# INFRA_GUIDE.md

## K8s 구성

- 런타임: OrbStack Kubernetes (로컬 Mac Mini)
- Namespace: quant-bot
- Deployment: trading-bot
- 이미지: ghcr.io/sambong90/trading_bot:latest
- Self-hosted runner: Mac Mini (GHA deploy job 실행)
- 이미지 pull 정책: Always — restart 시 자동으로 최신 이미지 pull

## Secret 구조

trading-bot-secret (개별 env):
- UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, TELEGRAM_*, DB_URL

trading-bot-secrets (envFrom, 통째로 주입):
- COPILOT_ACCESS_TOKEN, FLASK_API_KEY

새 환경변수 추가 시 trading-bot-secrets에 patch하면 자동으로 파드에 주입됨.

## CI/CD 흐름

배포는 git push만 한다. GitHub Actions가 빌드 → GHCR push → kubectl rollout restart까지 자동 처리.

금지:
- kubectl rollout restart 직접 실행 금지
- docker compose build / docker compose up 직접 실행 금지

git push 후 gh run view로 배포 검증.

## 봇 설정 상수

- 모니터링 티커: 60개 KRW 마켓
- MAX_OPEN_POSITIONS: 6
- Circuit Breaker 임계값: 일간 DD 5%
- Panic Dip-Buy: FNG <= 20 (Extreme Fear) + RSI <= 30 또는 BB하단 터치 시 발동
- BUY_COOLDOWN_MINUTES: 60분

## AI Reviewer

- API: GitHub Copilot API (https://api.githubcopilot.com)
- 인증: COPILOT_ACCESS_TOKEN (OAuth token, PAT 아님)
- 모델: gpt-4o
- 스케줄: 매주 일요일 KST 04:00 (auto_tuner 완료 후 순차 실행)
- APScheduler timezone: Asia/Seoul — hour=4는 KST 기준
