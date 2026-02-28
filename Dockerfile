# -----------------------------------------------------------------------
# Trading Bot — Dockerfile
# Base: python:3.11-slim (ARM64 native for M-series Mac / linux/arm64)
# WORKDIR /app  = workspace root
#   /app/trading_bot/db/   → SQLite DB (볼륨 마운트 권장)
#   /app/trading_bot/logs/ → 로그·캐시·pid 파일 (볼륨 마운트 권장)
# -----------------------------------------------------------------------
FROM python:3.11-slim

# 빌드 레이어 캐시 최적화: 환경 변수
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── 시스템 의존성 ──────────────────────────────────────────────────────
# gcc: numpy/pandas C 확장 컴파일용
# libgomp1: ta-lib 일부 variant에서 필요 (OpenMP)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Python 의존성 ──────────────────────────────────────────────────────
# requirements.txt만 먼저 복사 → 코드 변경 시 pip 레이어 재사용
COPY trading_bot/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── 애플리케이션 코드 복사 ────────────────────────────────────────────
COPY trading_bot/ trading_bot/

# ── .venv/bin/python 심링크 ───────────────────────────────────────────
# scheduler_service.py 내부에서 서브프로세스를 .venv/bin/python으로 호출함.
# Docker 이미지에는 venv가 없으므로, 시스템 Python을 가리키는 심링크로 투명하게 해결.
RUN mkdir -p .venv/bin \
    && ln -sf "$(which python3)" .venv/bin/python

# ── 퍼시스턴트 디렉터리 생성 ─────────────────────────────────────────
# 볼륨 마운트 없이 실행해도 오류 없도록 미리 생성.
RUN mkdir -p trading_bot/db trading_bot/logs

# ── 비루트 사용자 (보안) ──────────────────────────────────────────────
RUN addgroup --system botgroup \
    && adduser --system --ingroup botgroup --no-create-home botuser \
    && chown -R botuser:botgroup /app
USER botuser

# ── 진입점 ────────────────────────────────────────────────────────────
# scheduler_service.py가 메인 데몬:
#   - APScheduler (cron 기반 캔들 동기화 사이클)
#   - Telegram 봇 서브프로세스 기동
#   - DB 하우스키핑 / 튜너 / 시장 브리핑 스케줄
CMD ["python3", "trading_bot/tasks/scheduler_service.py"]
