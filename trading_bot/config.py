# [NEW] Trading Bot 설정 상수 (환경 변수로 오버라이드 가능)
import os
from pathlib import Path

# 로그/상태 파일 디렉터리 (paper_state.json 등)
LOGS_DIR = Path(__file__).resolve().parent / 'logs'

# RSI 필터 임계값 (환경 변수로 오버라이드 가능)
RSI_BUY_MIN = float(os.environ.get('RSI_BUY_MIN', '40'))   # 과매도 탈출 확인
RSI_BUY_MAX = float(os.environ.get('RSI_BUY_MAX', '65'))   # 과매수 진입 방지
RSI_SELL_MIN = float(os.environ.get('RSI_SELL_MIN', '70')) # 과매수 구간 매도 강화

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
HARD_STOP_LOSS_PCT = float(os.environ.get('HARD_STOP_LOSS_PCT', '-10.0'))

# 슬리피지 허용 범위 (%): 참조가격 대비 이 비율 초과 괴리 시 주문 거부
SLIPPAGE_GUARD_PCT = float(os.environ.get('SLIPPAGE_GUARD_PCT', '3.0'))

# 텔레그램 알림 레벨 (CRITICAL > TRADE > SUMMARY > OFF)
TELEGRAM_ALERT_LEVEL = os.environ.get('TELEGRAM_ALERT_LEVEL', 'TRADE')

# 텔레그램 관리자 인증 (숫자 user_id, 비어있으면 CHAT_ID만 체크)
TELEGRAM_ADMIN_USER_ID = os.environ.get('TELEGRAM_ADMIN_USER_ID', '')

# 스케줄러 캔들 마감 동기화 오프셋 (정시 후 N초에 실행, 기본 60초 = HH:01:00)
CANDLE_SYNC_OFFSET_SEC = int(os.environ.get('CANDLE_SYNC_OFFSET_SEC', '60'))
