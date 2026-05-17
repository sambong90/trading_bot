# CLAUDE.md — trading_bot 프로젝트 규칙

## 📜 전략 문서 위계 (Strategy Document Hierarchy)

트레이딩 로직은 아래 3개 파일의 상호작용으로 결정됩니다. 코딩 및 분석 시 반드시 아래 위계를 준수합니다.

| 우선순위 | 파일 | 역할 | 사용 시점 |
|----------|------|------|-----------|
| **1 (Primary)** | `research/data/strategies/core_logic_distilled.md` | 구현 명세서 — 최종 수치·임계값·If-Then 규칙 | 코드 생성·수정 전 **항상 먼저 로드** |
| **2 (Audit)** | `research/data/strategies/master_strategy_filtered.md` | 검증 지침서 — 폐기/최신화 맥락 기록 | 특정 수치의 변경 이유 확인 시만 참조 |
| **3 (Knowledge)** | `research/data/strategies/master_strategy.md` | 원본 지식 저장소 — 2022~2026 전문가 인사이트 | 정성적 패턴 상세 묘사·예외 상황 참고 시만 사용 |

### AI 에이전트 준수 규칙

- **코드 생성·수정 전**: `core_logic_distilled.md` 를 먼저 확인하여 현재 수치 기준을 파악한다.
- **수치 충돌 시**: `core_logic_distilled.md` 수치가 무조건 우선 (원본 `master_strategy.md` 수치 직접 사용 금지).
- **새 로직 발견 시**: `master_strategy.md`에서 코딩에 반영할 내용을 발견하면, 코드 수정 전 `core_logic_distilled.md` 업데이트를 먼저 사용자에게 제안한다.
- **과거 수치 사용 금지**: `master_strategy.md`에 기재된 구버전 수치(예: 교환비 1:500, 도미 40/41.3)는 코드에 직접 사용 불가.

---

## 비용 최적화 프로토콜

### 1. 코드 수정 방식 — Diff 우선
- 전체 파일을 다시 쓰지 않는다. **변경이 필요한 블록만** Edit 도구로 최소 단위 수정.
- 응답에 코드를 포함할 때는 수정된 부분만 발췌. 전후 컨텍스트는 3~5줄로 제한.

### 2. 파일 읽기 제한 — 타겟 분석
- `logs/`, `*.log`, `__pycache__/`, `.venv/`, `venv/`, `node_modules/` 절대 읽지 않음.
- `*.db`, `*.sqlite` 파일 직접 읽기 금지 (kubectl exec로 쿼리).
- 질문과 직접 연관된 파일만 열람. 연관성 불명확 시 파일명/함수명 먼저 확인 후 개방.
- 전체 파일 읽기 전 `Grep`으로 해당 함수·클래스 위치를 먼저 파악.

### 3. 컨텍스트 확장 억제 — 타겟 파일 우선순위
질문 유형별 우선 탐색 파일:

| 질문 유형 | 우선 탐색 |
|---|---|
| 매매 로직 | `auto_trader.py`, `balanced_plus.py` |
| 주문 체결 | `executor.py` |
| 스케줄/알림 | `scheduler_service.py`, `telegram_bot.py` |
| 리스크 관리 | `risk.py` |
| DB/모델 | `models.py`, `db.py` |
| 배포/인프라 | `k8s/`, `.github/workflows/` |

- 위 범위 밖 파일이 필요하면 **먼저 유저에게 확인** 후 열람.

### 4. 답변 압축 프로토콜
- 답변 구조: **수정 코드 → 핵심 이유 1~2줄** (설명 먼저 X)
- 원인 분석이 필요한 경우에도 3줄 이내로 압축.
- 이미 알려진 컨텍스트(이전 대화에서 확인된 사실)는 재설명하지 않음.
- 완료된 작업 요약, "~했습니다" 마무리 문장 생략.

## 기술 스택 및 개발 가이드라인

### 기술 스택
- **Runtime**: Python 3.11 (ARM64, python:3.11-slim)
- **거래소 연동**: pyupbit
- **스케줄러**: APScheduler (`BackgroundScheduler`, timezone=Asia/Seoul)
- **DB**: PostgreSQL + SQLAlchemy ORM (Alembic 없음, 수동 DDL)
- **API 서버**: Flask 3.x
- **알림**: Telegram Bot API
- **배포**: OrbStack K8s + GitHub Actions + GHCR

---

### 1. 코딩 표준

**네이밍**
- 함수·변수: `snake_case` / 상수: `UPPER_SNAKE_CASE` / 클래스: `PascalCase`
- 프라이빗 함수: `_underscore` prefix
- 불리언 변수: `is_`, `has_`, `can_` prefix 사용

**파일 구조**
```
trading_bot/
  models.py         # DB 모델 (ORM)
  db.py             # 세션 팩토리
  config.py         # 환경변수 파싱 (os.environ.get 집중)
  executor.py       # 주문 실행 (Paper / Live)
  risk.py           # 리스크·CB 로직
  balanced_plus.py  # 전략 상수 및 시그널 생성
  tasks/
    auto_trader.py        # 매매 사이클
    scheduler_service.py  # APScheduler 등록
    market_briefing.py    # 브리핑 서브프로세스
    ai_reviewer.py        # 주간 AI 리뷰
```

**코드 스타일**
- 함수 길이: 50줄 초과 시 분리 검토
- 매직 넘버 금지 → `config.py` 또는 `balanced_plus.py` 상수로 정의
- 환경변수는 반드시 `config.py`에서 파싱, 다른 파일에서 `os.environ.get` 직접 호출 지양

---

### 2. 에러 핸들링 전략

**원칙**
- 외부 I/O (Upbit API, Telegram, DB) 는 **반드시** `try/except` 포함
- 루프 내 에러는 **개별 항목 단위**로 catch — 전체 루프 중단 금지
- `except Exception` 사용 시 반드시 `logger.warning/error`로 기록
- 비즈니스 로직 에러 (`RuntimeError`, `ValueError`)는 catch 후 재전파 (`raise`)
- 서브프로세스 실패는 stderr 출력 + `sys.exit(1)` (stdout은 무시됨)

**DB 세션 패턴**
```python
session = get_session()
try:
    # 작업
    session.commit()
except Exception as e:
    session.rollback()
    logger.error('설명: %s', e)
finally:
    session.close()
```

**금지 패턴**
```python
except Exception:
    pass  # ← 금지. 최소 logger.debug라도 남길 것
```

---

### 3. 테스트 및 검증

**코드 수정 후 필수 검증 순서**

```bash
# 1. 문법·임포트 오류 확인
python -m py_compile trading_bot/<수정파일>.py

# 2. 전체 모듈 임포트 체인 확인
python -c "from trading_bot.<모듈명> import *"

# 3. 파드 내 실제 동작 확인 (DB 연결 포함)
kubectl exec -n quant-bot deployment/trading-bot -- python3 -c "<검증코드>"

# 4. 배포 후 로그 확인
kubectl logs -n quant-bot deployment/trading-bot --tail=50 -f
```

**변경 유형별 필수 검증**

| 변경 유형 | 필수 검증 |
|---|---|
| executor.py | 주문 로직 유닛 검증 (PaperExecutor로 시뮬레이션) |
| models.py | `get_session()` 후 쿼리 실행으로 스키마 정합성 확인 |
| scheduler_service.py | 파드 재시작 후 job 등록 로그 확인 |
| risk.py | CB 조건 경계값 수동 계산 대조 |
| telegram_bot.py | `/balance` 등 커맨드 실제 실행 |

**배포 검증**
```bash
gh run watch          # GHA 빌드·배포 완료 대기
kubectl rollout status deployment/trading-bot -n quant-bot
```

---

## 에이전트 워크플로우

코드 생성 또는 대대적인 수정 시, 내부적으로 3단계를 거쳐 최종 결과물을 도출한다.

| 역할 | 책임 |
|---|---|
| **[Architect]** | 요구사항 분석 → 설계 구조 결정 (확장성, 의존성, 부작용 파악) |
| **[Dev]** | 설계 기반 최소 Diff 코드 작성 (토큰 최적화 준수) |
| **[QA]** | 예외 처리·보안 취약점·에러 가능성 검토 후 최종 승인 |

### 출력 형식
```
[Architect] 설계 방향 1~2줄
[Dev] 구현 포인트 1줄
[QA] 검토 결과 1줄 (이슈 있으면 수정 후 재승인)

→ 최종 코드 (Diff)
```

- 단순 버그 픽스·1줄 수정은 워크플로우 생략, 바로 Diff 제시.
- 각 단계는 1~2줄로 압축. 장황한 설명 금지.

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
