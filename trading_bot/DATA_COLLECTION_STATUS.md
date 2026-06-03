# DATA_COLLECTION_STATUS

전 종목 × 전 타임프레임 OHLCV 풀 수집 — 설계·현황 (2026-06-03, KST)

연구 모드(신규 매수 중단)에서 향후 전략 설계용 데이터를 폭넓게 축적한다.
매매·청산 로직은 불변, 데이터 수집 잡만 추가한다.

## 1. 종목 커버리지

- Upbit KRW 거래 가능 종목: 262개 (pyupbit.get_tickers(fiat=KRW), 실측).
- 기존 ohlcv 누적 distinct 종목: 232개.
  - minute60: 227종목 / day: 227종목 / minute240: 94종목.
  - 232는 최근 3개월간 거래대금 top-N(60~80)에 한 번이라도 든 종목의 누적. 매 사이클 실제 수집은 top-60뿐.
- 누락: Upbit 262 − DB 232 ≈ 30종목. 3개월간 한 번도 top-N에 들지 못한 저거래대금 종목(미수집).
- 조치: get_all_krw_tickers_full()로 top-N 절단을 해제 → 거래 가능 KRW 전 종목을 수집 대상으로.
  스테이블코인(KRW-USDT 등)·known_delisted는 제거. BTC/USDT 마켓은 제외(연구 1차는 KRW만).

## 2. 타임프레임별 수집 현황·계획

기존(유지):
- minute60: 매매 사이클이 매분 top-60 수집(227종목 누적). 신규로 전 종목 매시 보완 추가.
- minute240(4h): 6회/일, 기존 top-80 → 전 종목으로 확대.
- day: 매매/브리핑 경로로 수집(227종목). 신규로 전 종목 1일 1회 보완.

신규:
- minute1: 5분마다 최근 200봉 묶음, 전 종목. (매분 호출 대비 API 1/5, 저장은 1분 해상도 유지)
- minute15: 봉마감+2분(매시 02/17/32/47), 전 종목.
- minute30: 봉마감+3분(매시 03/33), 전 종목.
- minute60 전 종목 보완: 매시 03분 (매매 사이클이 못 채우는 비-top-60 종목).
- week: 1일 1회 08:12, 단일 호출로 장기 백필(~3.8년).
- month: 1일 1회 08:14, 단일 호출로 장기 백필(~16년).
- 부팅 직후 +60~600초 시차로 7종 1회 백필 예약(rate-limit 분산).

깊은 1분봉 백필(페이징 200개 단위 과거 소급)은 1차 범위에서 제외 — 현재는 최근 200봉 부트스트랩 후 전진 누적.
필요 시 별도 백필 스크립트로 단계 확장.

## 3. 디스크 사용량·예상 증가율

- 노드 디스크: 127GB 총, 115GB free (PVC 명목 4Gi는 local-path라 미강제, 실제 노드 디스크 사용).
- 현 ohlcv: 92MB / 122,403행 → 792 B/row(힙+인덱스 4종 포함, 실측).
- 예상 증가(792B/row 기준):
  - minute1 전 종목: 262 × 1440 = 377,280행/일 ≈ 298MB/일 ≈ 9.0GB/월.
    180일 롤링 plateau ≈ 54GB.
  - minute15: 262 × 96 = 25,152행/일 ≈ 20MB/일. 365일 ≈ 7.3GB.
  - minute30: 262 × 48 ≈ 9.9MB/일. 365일 ≈ 3.6GB.
  - minute60 전 종목(무기한): ≈ 1.8GB/년. minute240 ≈ 0.45GB/년. day/week/month: 무시 가능.
- 총 plateau ≈ 65GB + 저해상도 ~2GB/년 < free 115GB. 여유 확보.
- 주의: "무기한 보존 가능"은 1분봉을 180일로 제한했을 때 성립. 1분봉 무기한 보존 시 ~106GB/년으로 ~1년 내 포화.

## 4. API rate limit 여유

- Upbit quotation(캔들/티커): 10 req/s, 600 req/min (IP 그룹).
- minute1 수집: 262종목 × (내부 sleep 0.1~0.3s + COLLECT_SLEEP_SEC 0.15s) ≈ 2분 분산 → 평균 <3 req/s.
- 매매 사이클(청산 모니터, 매분 top-60) + 분봉 수집 합산 피크도 분 단위 한도(600) 내.
- 분 오프셋 분리(1m=:*/5+30s, 15m=:02/17/32/47, 30m=:03/33, 1h보완=:03, day/week/month=새벽 시차)로 동시 버스트 회피.
- 429 감지 시 fetch_ohlcv 내부 지수 백오프 기존 보유.

## 5. 1분봉 수집 방식 (매분 vs 5분 배치)

- 채택: 5분 배치. 5분마다 최근 200봉을 묶어 수집 → 1분 해상도 데이터는 동일하게 확보하되 호출 빈도 1/5.
- 근거: fetch_ohlcv(use_db_first=True)는 DB 신선 시 최근 50봉만 API로 받아 병합(ON CONFLICT DO NOTHING).
  5분 간격이면 직전 5봉만 신규 → 누락 없이 1분 해상도 유지. 매분 호출 대비 API·DB 부하 대폭 절감.
- max_instances=1로 직전 패스 미완료 시 다음 패스 스킵(파일업 방지).

## 6. 보관 정책

- 타임프레임별 분리(db_maintenance, env로 조정):
  - minute1: 180일 롤링(OHLCV_PRUNE_DAYS_1M). 디스크 보호 기본값. 6개월 분봉이면 초기 연구 충분.
  - minute15/minute30: 365일(OHLCV_PRUNE_DAYS_INTRADAY).
  - minute60/minute240/day/week/month: 무기한(OHLCV_PRUNE_DAYS_DEFAULT=0). 저용량·고가치 장기 축적.
- 기존 정책(전 OHLCV 90일 일괄 삭제)을 대체. 저해상도가 90일에서 잘리던 문제 해소(백테스트 히스토리 확대).
- 안전판: _check_disk_capacity가 6시간마다 사용률 점검, 80% 초과 시 텔레그램 알림 → 보관일 하향 유도.

## 검증 체크리스트(배포 후)

- [ ] get_all_krw_tickers_full 반환 ≈ 262종목(스테이블 제외).
- [ ] collect_1m/15m/30m/60m_full/day/week/month 잡 등록 로그 확인.
- [ ] 부팅 백필 후 ohlcv timeframe별 신규 행(minute1/minute15/minute30/week/month) 생성 확인.
- [ ] 1분봉 수집 1패스 소요·실패율 로그, rate-limit 429 부재.
- [ ] 일일 ohlcv 증가량 실측 → 예상(≈9GB/월 1m) 대조.
- [ ] 매매(청산) 사이클 정상, 수집과 독립 동작.
- [ ] disk_capacity 잡 사용률 로그 출력.
