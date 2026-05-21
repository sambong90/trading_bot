# CODING_STANDARDS.md

## 네이밍 컨벤션

- 함수·변수: snake_case / 상수: UPPER_SNAKE_CASE / 클래스: PascalCase
- 프라이빗 함수: _underscore prefix
- 불리언 변수: is_, has_, can_ prefix

## 파일 구조

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

## 코드 스타일

- 함수 길이: 50줄 초과 시 분리 검토
- 매직 넘버 금지 → config.py 또는 balanced_plus.py 상수로 정의
- 환경변수는 반드시 config.py에서 파싱, 다른 파일에서 os.environ.get 직접 호출 지양

## 에러 핸들링 전략

- 외부 I/O (Upbit API, Telegram, DB) 는 반드시 try/except 포함
- 루프 내 에러는 개별 항목 단위로 catch — 전체 루프 중단 금지
- except Exception 사용 시 반드시 logger.warning/error로 기록
- 비즈니스 로직 에러 (RuntimeError, ValueError)는 catch 후 재전파 (raise)
- 서브프로세스 실패는 stderr 출력 + sys.exit(1)

## DB 세션 패턴

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

금지 패턴:
```python
except Exception:
    pass  # 금지. 최소 logger.debug라도 남길 것
```

## 테스트 검증 순서

코드 수정 후 필수 검증 순서:

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

변경 유형별 필수 검증:
- executor.py: 주문 로직 유닛 검증 (PaperExecutor로 시뮬레이션)
- models.py: get_session() 후 쿼리 실행으로 스키마 정합성 확인
- scheduler_service.py: 파드 재시작 후 job 등록 로그 확인
- risk.py: CB 조건 경계값 수동 계산 대조
- telegram_bot.py: /balance 등 커맨드 실제 실행

배포 검증:
```bash
gh run watch
kubectl rollout status deployment/trading-bot -n quant-bot
```

## 에이전트 워크플로우

코드 생성 또는 대대적인 수정 시 내부적으로 3단계를 거쳐 최종 결과물을 도출한다.

- [Architect]: 요구사항 분석 → 설계 구조 결정 (확장성, 의존성, 부작용 파악)
- [Dev]: 설계 기반 최소 Diff 코드 작성 (토큰 최적화 준수)
- [QA]: 예외 처리·보안 취약점·에러 가능성 검토 후 최종 승인

출력 형식:
```
[Architect] 설계 방향 1~2줄
[Dev] 구현 포인트 1줄
[QA] 검토 결과 1줄 (이슈 있으면 수정 후 재승인)

→ 최종 코드 (Diff)
```

단순 버그 픽스·1줄 수정은 워크플로우 생략, 바로 Diff 제시.

## 질문 유형별 우선 탐색 파일

- 매매 로직: auto_trader.py, balanced_plus.py
- 주문 체결: executor.py
- 스케줄/알림: scheduler_service.py, telegram_bot.py
- 리스크 관리: risk.py
- DB/모델: models.py, db.py
- 배포/인프라: k8s/, .github/workflows/
