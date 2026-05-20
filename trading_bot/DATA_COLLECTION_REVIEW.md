# 데이터 수집 주기 전면 점검 보고서

작성일: 2026-05-20  
목적: 데이터 수집 체계 현황 파악 및 개선 방향 도출  
제약: 분석/제안만 포함 — 코드 수정 없음

---

## Part 1: 현재 상태

### 1-1. 데이터 소스별 현황

**매크로 (DXY/NDQ/Gold/Bond/JPY/Oil)**
- 소스: Yahoo Finance (yfinance) — `yf.download(period='5d', interval='1d')`
- 수집 주기: 1회/일 (07:00 KST, `cron hour=7, minute=0`)
- Stale 임계값: 26h → STALE_BUT_USABLE (buy_size_multiplier=0.5), 72h → is_tradeable=False
- 소비처: MarketGuardian L1 플래그 G-01~G-13 (DXY 급등, NASDAQ 급락, 금 급등, 유가 변동성 등)
- API 제한: 비공식 API, rate limit 불명확 (공식 문서 없음)
- DB 저장: macro_snapshots 테이블 (ratio_quality 필드 포함, 주말이면 'stale' 기록)
- 수집 실패 패턴: DXY/NDX 데이터 없으면 None 반환, 예외 발생 시 텔레그램 알림, **None 반환 시 텔레그램 알림 없음**

**BTC 도미넌스**
- 소스: CoinGecko `/api/v3/global` (API Key 불필요)
- 수집 주기: 6회/일 (00:02, 04:02, 08:02, 12:02, 16:02, 20:02 KST)
- Stale 임계값: None 값이면 is_tradeable=False (DOMINANCE_DATA_MISSING), **나이 기반 stale 체크 없음**
- 소비처: MarketGuardian L2 레짐 판정 (bull_stage → BULL_EARLY/CONFIRMED/CLIMAX/BEAR_*)
- API 제한: 무료 tier 10~30 req/min, 6회/일은 여유로움
- 수집 실패 패턴: 3회 재시도 (지수 백오프 2^attempt초)

**김프 (KRW Premium)**
- 소스: Upbit KRW-BTC + Binance BTCUSDT + ExchangeRate-API (open.er-api.com)
- 수집 주기: 24회/일 (매시 00분, `cron minute=0`)
- Stale 임계값: **없음** — 최신값이 항상 있으면 그대로 사용
- 소비처: MarketContext 딕셔너리에 포함되어 전달되지만 **MarketGuardian.evaluate()에서 실제 소비 안 함** (표시·로깅 전용)
- API 제한: ExchangeRate-API 무료 ~1,500 req/month ≈ 50/day, 24회/일은 48% 소진
- 수집 실패 패턴: 3회 재시도 (1.5^attempt초), 환율 실패 시 fallback=1350

**공포탐욕지수 (FNG)**
- 소스: alternative.me `https://api.alternative.me/fng/?limit=1&format=json`
- 수집 주기: **스케줄 없음** — auto_trader.py run_cycle()에서 매 사이클마다 live 호출
- Stale 임계값: 없음 — 실패 시 fallback=50 (Neutral)
- 소비처: strategy.py PANIC_DIP 패턴 판단 (FNG < 20이면 extreme_fear)
- API 제한: 비공식, 사실상 무제한 (일 수백~수천 요청 허용)
- DB 저장: **없음** — 5분 메모리 캐시만 (pod 재시작 시 캐시 초기화)
- 수집 실패 패턴: fallback=50 반환 (에러 로그만, 알림 없음)

**BTC 주봉 200MA**
- 소스: Upbit via pyupbit `get_ohlcv('KRW-BTC', interval='week', count=210)`
- 수집 주기: **스케줄 없음** — aggregator.py `_check_btc_weekly_200()` 내에서 on-demand 호출 (매 사이클)
- Stale 임계값: 없음 — 호출 실패 시 None 반환 (G-14 ALT_MASSACRE 체크 스킵)
- 소비처: MarketGuardian G-14 (BTC 주봉 200MA 하회 시 ALT 매매 차단)
- API 제한: pyupbit 약 10 req/s, 주봉 210개 요청 1회로 충분

### 1-2. DB 현재 상태 (2026-05-20 19:02 KST 기준)

- macro: 마지막 수집 05/20 07:00 KST, 나이 12.3h → FRESH
- dominance: 마지막 수집 05/20 16:02 KST, 나이 3.3h → FRESH
- kimp: 마지막 수집 05/20 19:00 KST, 나이 0.3h → FRESH

---

## Part 2: 문제점

### 문제 1 — 매크로 무음 실패 (심각도: 높음)

`collect_macro()`가 DXY/NDX 데이터를 야후에서 가져오지 못하면 `None`을 반환한다.  
scheduler_service.py는 이 경우 `_log('warning')` 만 출력하고 텔레그램 알림(`_notify_scheduler()`)을 호출하지 않는다.  
예외 발생 시에는 알림이 가지만 silent None 반환은 완전히 무시된다.

결과: 수집이 실패해도 운영자가 모른다. 다음 날 07:00 재수집 전까지 26h를 넘으면 STALE_BUT_USABLE, 72h 초과 시 거래 차단.

### 문제 2 — 매크로 1회/일 + 협소한 유효 윈도우 (심각도: 높음)

수집 주기 1회/일 (07:00 KST) + stale 임계값 26h = 실질 허용 실패 시간 **2h** (07:00 → 다음날 07:00 = 24h, 24h + 2h = 26h).  
즉, 오늘 07:00 수집이 실패하면 **내일 09:00 KST부터 매수 사이즈가 반으로 줄어든다**.  
야후 파이낸스는 비공식 API라 일시 장애가 빈번하다. 1회/일 수집은 복원 기회가 전혀 없다.

### 문제 3 — 김프 과잉 수집 + 미활용 (심각도: 낮음)

24회/일 수집하지만 MarketGuardian이 실제로 소비하지 않는다.  
ExchangeRate-API 무료 할당량의 48%를 김프에 사용 중.  
수집 주기를 절반만 줄여도 API 여유가 생긴다.

### 문제 4 — FNG 비지속성 (심각도: 중간)

DB에 저장되지 않으므로 pod 재시작, 장애 복구 후 이전 FNG 이력이 없다.  
메모리 캐시(5분)가 프로세스 단위라 auto_trader.py가 subprocess로 실행될 때마다 캐시가 초기화된다.  
fallback=50이 항상 작동하므로 거래 차단은 없지만, extreme_fear 국면에서 pod 장애가 생기면 PANIC_DIP 신호를 놓친다.

### 문제 5 — 도미넌스 나이 체크 없음 (심각도: 낮음)

dominance 테이블에 최신 레코드가 있으면 나이에 상관없이 사용된다.  
6회/일 수집으로 최대 나이는 4h이므로 현실적 위험은 낮다.  
다만 CoinGecko 장기 장애 시 수 일이 지난 데이터를 사용할 수 있다.

### 문제 6 — BTC 주봉 200MA 매 사이클 호출 (심각도: 낮음)

주봉 200MA는 주 단위로만 바뀌는데 매 사이클(1h)마다 pyupbit API를 호출한다.  
현재 부하가 크지 않지만, 사이클 빈도가 올라가거나 pyupbit 장애 시 G-14 체크가 매번 실패한다.

---

## Part 3: 권장 수집 주기

### 매크로

현재: 1회/일 (07:00 KST)  
권장: **2회/일** — 07:00 KST + 19:00 KST (12h 간격)

근거:
- 12h 간격이면 한 번 실패해도 다음 수집까지 12h, stale 임계값(26h)까지 여유 14h
- 야후 파이낸스는 장 마감 후 데이터(US 기준 16:00 EST = KST 06:00)가 확정되므로 07:00 수집은 그대로 유지
- 19:00 KST 추가 수집은 US 장중 데이터(부분 업데이트)를 반영
- yfinance rate limit 우려 없음 — 2회/일은 최소 요청

실패 알림: None 반환 시에도 텔레그램 알림 추가 필요 (코드 수정 제안, 실제 수정 불포함)

### BTC 도미넌스

현재: 6회/일 (4h 간격)  
권장: **유지** — 6회/일 4h 간격이 적정

근거:
- 도미넌스는 시간 단위로 의미 있게 변화
- CoinGecko 무료 tier에 여유 충분
- 나이 stale 체크(예: 8h 이상이면 DOMINANCE_STALE 경고) 추가 권장

### 김프

현재: 24회/일 (매시)  
권장: **4회/일** — 00:00, 06:00, 12:00, 18:00 KST

근거:
- MarketGuardian이 소비하지 않으므로 실시간성이 불필요
- ExchangeRate-API 절약: 24 → 4회 = 83% 감소 (할당량 8% 사용)
- 김프는 표시 목적이므로 6h 단위 갱신으로 충분
- 향후 Guardian이 김프를 소비하게 될 경우 주기 재검토

### FNG

현재: 스케줄 없음, 매 사이클 live 호출, DB 저장 없음  
권장: **스케줄 추가 + DB 저장**

방향:
- 1회/시간 스케줄 수집 + DB 저장 (sentiment_snapshots 신규 테이블 또는 기존 확장)
- auto_trader.py는 DB에서 최신값을 읽어 사용 (live 호출 대신)
- DB 값 나이가 2h 초과하면 live 폴백 유지 (현행 로직 보존)

이점:
- pod 재시작 후에도 최근 FNG 이력 유지
- PANIC_DIP 신호 누락 방지
- alternative.me API 호출 횟수: 24/일(스케줄) vs 현재 ~24/일(사이클별) — 동일

### BTC 주봉 200MA

현재: 매 사이클 on-demand 호출  
권장: **1회/일 스케줄 수집 + 캐시**

방향:
- 매일 08:00 KST 1회 수집 후 DB 또는 Redis에 저장
- aggregator.py는 저장된 값을 읽어 사용
- 주봉 데이터는 일 단위 갱신으로 충분

이점:
- pyupbit API 호출 횟수 대폭 감소 (매 사이클 → 1회/일)
- pyupbit 장애 시 G-14 체크 실패 위험 제거

---

## Part 4: 실패 복원력 강화 제안

### 제안 A — 매크로 무음 실패 알림 (우선순위: 높음)

`scheduler_service.py`의 `collect_macro()` 호출부에서:
- 반환값이 None이면 `_notify_scheduler('[collect_macro] 수집 실패: DXY/NDX 데이터 없음')` 호출
- 현재는 예외만 알림, None 반환은 무시 → 동일한 알림 경로 적용 필요

구현 위치: `scheduler_service.py` `_do_collect_macro()` 래퍼 함수 (현재 미존재 — 직접 호출부에 추가)

### 제안 B — 매크로 재수집 트리거 (우선순위: 높음)

알림만으로는 부족. 수집 실패 시 자동 재시도 스케줄 필요:
- 실패 감지 → 30분 후 재시도 (1회)
- 재시도도 실패 → 텔레그램 알림 후 포기 (다음 정규 수집까지 대기)

APScheduler에서 one-shot 재시도 job 추가로 구현 가능.

### 제안 C — 도미넌스 나이 경고 (우선순위: 낮음)

aggregator.py에서 dominance 나이가 8h 이상이면 `DOMINANCE_STALE` 컨텍스트 플래그 추가.  
현재 None 체크만 있으므로 나이 체크 레이어 추가 시 이중 방어.

### 제안 D — 수집 건강 체크 엔드포인트 (우선순위: 중간)

현재 데이터 나이 확인은 kubectl exec로만 가능. FastAPI 앱에 `/health/data` 엔드포인트 추가:
```
GET /health/data
{
  "macro_age_h": 12.3,
  "macro_status": "FRESH",
  "dominance_age_h": 3.3,
  "dominance_status": "FRESH",
  "kimp_age_h": 0.3,
  "kimp_status": "FRESH"
}
```
운영 모니터링 편의성 대폭 향상.

### 제안 E — ExchangeRate-API 여유 확보 (우선순위: 낮음)

김프 수집을 24 → 4회/일로 줄이면 ExchangeRate-API 월 할당량의 92%가 해방됨.  
이 여유로 매크로 수집 2회/일 추가 시에도 환율 API는 영향 없음.  
(매크로는 yfinance이므로 ExchangeRate-API와 관계 없음 — 단순 계획 여유 확인)

---

## 요약

긴급도 순 개선 우선순위:

1. **매크로 무음 실패 알림** — None 반환 시 텔레그램 미발송, 운영자 모름 (즉시 수정 필요)
2. **매크로 2회/일로 증가** — 1회 실패 시 복원 기회 확보 (유효 윈도우 2h → 14h)
3. **FNG DB 저장 + 스케줄** — pod 재시작 후 extreme_fear 국면 신호 누락 방지
4. **김프 4회/일로 감소** — 미활용 데이터에 API 할당량 낭비 제거
5. **BTC 주봉 200MA 캐싱** — 매 사이클 API 호출 제거, G-14 안정성 향상
6. **도미넌스 나이 체크** — 장기 CoinGecko 장애 대비 나이 경고 레이어 추가
7. **건강 체크 엔드포인트** — kubectl exec 없이 데이터 상태 즉시 확인
