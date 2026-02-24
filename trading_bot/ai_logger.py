"""
AI 전용 분석 로그 — LLM 검증용. 전략 판단·매매 액션만 기록하며 운영 로그와 분리.
출력: trading_bot/logs/ai_debug.log (| 구분 정형 포맷)
"""
import logging
from pathlib import Path

# trading_bot/logs 경로 (이 모듈 기준: trading_bot/ai_logger.py → trading_bot/logs)
_LOGS_DIR = Path(__file__).resolve().parent / 'logs'
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_AI_LOG_FILE = _LOGS_DIR / 'ai_debug.log'

ai_logger = logging.getLogger('trading_bot.ai')
ai_logger.setLevel(logging.INFO)
# 기존 핸들러가 붙어 있으면 중복 방지 (다른 모듈에서 import 시 재사용)
if not ai_logger.handlers:
    _handler = logging.FileHandler(_AI_LOG_FILE, encoding='utf-8')
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    ai_logger.addHandler(_handler)
    ai_logger.propagate = False  # 루트 로거로 전파하지 않음 (콘솔 미출력)

