"""
AI 전용 분석 로그 — LLM 검증 및 자가 진화(Self-evolving) 평가용.
전략 판단·매매 액션만 기록하며 운영 로그와 분리.

출력:
  - trading_bot/logs/ai_debug.log    (기존 파이프 구분 포맷 — 하위 호환)
  - trading_bot/logs/ai_analysis.jsonl (JSON Lines — AI 파싱 최적화)
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# trading_bot/logs 경로
_LOGS_DIR = Path(__file__).resolve().parent / 'logs'
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_AI_LOG_FILE = _LOGS_DIR / 'ai_debug.log'
_AI_JSONL_FILE = _LOGS_DIR / 'ai_analysis.jsonl'

# --- 기존 파이프 구분 텍스트 로거 (하위 호환) ---
ai_logger = logging.getLogger('trading_bot.ai')
ai_logger.setLevel(logging.INFO)
if not ai_logger.handlers:
    _handler = logging.FileHandler(_AI_LOG_FILE, encoding='utf-8')
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    ai_logger.addHandler(_handler)
    ai_logger.propagate = False


def log_ai_event(
    event_type: str,
    ticker: str,
    signal: str = None,
    price: float = None,
    avg_buy_price: float = None,
    regime: str = None,
    timeframe: str = None,
    adx: float = None,
    rsi: float = None,
    atr: float = None,
    vol_ratio: float = None,
    position_size: float = None,
    size_pct: float = None,
    decision_reason: str = None,
    roi: float = None,
    api_status: str = None,
    extra: dict = None,
):
    """
    AI 분석용 정형화된 JSON 로그 기록.

    Parameters:
        event_type: 'STRATEGY' | 'EXECUTE' | 'SKIP' | 'ERROR' | 'STOP_LOSS' | 'DCA' | 'SCALE_OUT'
        ticker: 코인 티커 (e.g. 'KRW-BTC')
        signal: 'buy' | 'sell' | 'hold'
        price: 현재가
        avg_buy_price: 평균 매수가 (진입가)
        regime: 'trend' | 'range' | 'weakening_trend' | 'transition'
        timeframe: 'minute60' | 'day' 등
        adx: ADX 값
        rsi: RSI 값
        atr: ATR 값
        vol_ratio: 거래량 비율 (현재봉/MA)
        position_size: 포지션 크기 (KRW)
        size_pct: 비중 (0~1)
        decision_reason: 판단 사유 문자열
        roi: 현재 수익률 (%)
        api_status: API 응답 상태 ('ok' | 'error' | 'timeout' 등)
        extra: 추가 키-값 (dict)
    """
    record = {
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'event': event_type,
        'ticker': ticker,
        'signal': signal,
        'price': price,
        'avg_buy_price': avg_buy_price,
        'roi_pct': round(roi, 2) if roi is not None else None,
        'regime': regime,
        'timeframe': timeframe,
        'adx': round(adx, 1) if adx is not None else None,
        'rsi': round(rsi, 1) if rsi is not None else None,
        'atr': round(atr, 2) if atr is not None else None,
        'vol_ratio': round(vol_ratio, 2) if vol_ratio is not None else None,
        'position_size_krw': round(position_size, 0) if position_size is not None else None,
        'size_pct': round(size_pct, 4) if size_pct is not None else None,
        'decision_reason': decision_reason,
        'api_status': api_status,
    }
    if extra:
        record['extra'] = extra

    # None 값 제거 (JSONL 파일 크기 최적화)
    record = {k: v for k, v in record.items() if v is not None}

    try:
        with open(_AI_JSONL_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass

    # 기존 파이프 구분 로거에도 기록 (하위 호환)
    try:
        ai_logger.info(
            "[%s] %s | Signal:%s | Price:%s | Regime:%s | ADX:%s | RSI:%s",
            event_type, ticker, signal or '-',
            f'{price:.0f}' if price and price >= 10 else f'{price:.4f}' if price else '-',
            regime or '-',
            f'{adx:.1f}' if adx is not None else '-',
            f'{rsi:.1f}' if rsi is not None else '-',
        )
    except Exception:
        pass
