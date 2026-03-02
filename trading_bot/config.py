# [NEW] Trading Bot 설정 상수 (환경 변수로 오버라이드 가능)
import os
from pathlib import Path

# 로그/상태 파일 디렉터리 (paper_state.json 등)
LOGS_DIR = Path(__file__).resolve().parent / 'logs'

# RSI 필터 임계값 (환경 변수로 오버라이드 가능)
RSI_BUY_MIN = float(os.environ.get('RSI_BUY_MIN', '40'))   # 과매도 탈출 확인
RSI_BUY_MAX = float(os.environ.get('RSI_BUY_MAX', '75'))   # 과매수 진입 방지
RSI_SELL_MIN = float(os.environ.get('RSI_SELL_MIN', '80')) # 과매수 구간 매도 강화

# [NEW] ATR 기반 Scale-Out 배수 (환경 변수 오버라이드 가능)
SCALE_OUT_ATR_MULT_1 = float(os.environ.get('SCALE_OUT_ATR_MULT_1', '2.0'))   # 1차 청산
SCALE_OUT_ATR_MULT_2 = float(os.environ.get('SCALE_OUT_ATR_MULT_2', '3.5'))   # 2차 청산
SCALE_OUT_ROI_FALLBACK_1 = float(os.environ.get('SCALE_OUT_ROI_FALLBACK_1', '5.0'))
SCALE_OUT_ROI_FALLBACK_2 = float(os.environ.get('SCALE_OUT_ROI_FALLBACK_2', '10.0'))

# [NEW] 변동성 스케일링 임계값
VOL_SCALE_HIGH = float(os.environ.get('VOL_SCALE_HIGH', '2.0'))  # ATR 2배 이상 → 50% 축소
VOL_SCALE_MID = float(os.environ.get('VOL_SCALE_MID', '1.5'))     # ATR 1.5배 이상 → 75% 축소

# [NEW] 트레일링 스탑 ATR 배수 (수익 구간별)
TS_MULT_LOW = float(os.environ.get('TS_MULT_LOW', '3.0'))   # ROI 0~5%: 느슨하게
TS_MULT_MID = float(os.environ.get('TS_MULT_MID', '2.0'))   # ROI 5~15%: 중간
TS_MULT_HIGH = float(os.environ.get('TS_MULT_HIGH', '1.5')) # ROI 15%+: 타이트하게

# 하드 스탑로스 (ROI %): 이 수치 이하 하락 시 전량 시장가 매도
HARD_STOP_LOSS_PCT = float(os.environ.get('HARD_STOP_LOSS_PCT', '-15.0'))

# 슬리피지 허용 범위 (%): 참조가격 대비 이 비율 초과 괴리 시 주문 거부
SLIPPAGE_GUARD_PCT = float(os.environ.get('SLIPPAGE_GUARD_PCT', '3.0'))

# 텔레그램 알림 레벨 (CRITICAL > TRADE > SUMMARY > OFF)
TELEGRAM_ALERT_LEVEL = os.environ.get('TELEGRAM_ALERT_LEVEL', 'TRADE')

# 텔레그램 관리자 인증 (숫자 user_id, 비어있으면 CHAT_ID만 체크)
TELEGRAM_ADMIN_USER_ID = os.environ.get('TELEGRAM_ADMIN_USER_ID', '')

# 스케줄러 캔들 마감 동기화 오프셋 (정시 후 N초에 실행, 기본 60초 = HH:01:00)
CANDLE_SYNC_OFFSET_SEC = int(os.environ.get('CANDLE_SYNC_OFFSET_SEC', '60'))

# 거시 트렌드 필터(Macro EMA) 일봉 기간.
# auto_tuner가 [5, 20, 30, 50, 100] 중 최적값을 TuningRun에 저장 → param_manager가 읽음.
# 튜닝 데이터가 없을 때의 최종 fallback.
MACRO_EMA_LONG = int(os.getenv('MACRO_EMA_LONG', 50))

# Drawdown Circuit Breaker 임계값 (%)
DD_DAILY_LIMIT_PCT = float(os.environ.get('DD_DAILY_LIMIT_PCT', '5.0'))
DD_TOTAL_LIMIT_PCT = float(os.environ.get('DD_TOTAL_LIMIT_PCT', '15.0'))

# Breakeven Stop: 이 ROI(%) 도달 후 트레일링 스탑 하한을 avg_buy로 고정
BREAKEVEN_ROI_PCT = float(os.environ.get('BREAKEVEN_ROI_PCT', '3.0'))

# Volatility Targeting: 포지션 사이징 목표 변동성 (연율화)
TARGET_VOL_PCT = float(os.environ.get('TARGET_VOL_PCT', '0.02'))

# Fear & Greed Index 임계값
FNG_EXTREME_FEAR = int(os.environ.get('FNG_EXTREME_FEAR', '20'))

# Panic Dip-Buy 포지션 비중 (MTF 하락장 + Extreme Fear 시 보수적 매수)
PANIC_DIP_BUY_SIZE_PCT = float(os.environ.get('PANIC_DIP_BUY_SIZE_PCT', '0.3'))

# Multi-TF 4h Confluence 활성화
MTF_4H_ENABLED = os.environ.get('MTF_4H_ENABLED', 'true').lower() in ('1', 'true', 'yes')
