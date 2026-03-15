#!/usr/bin/env python3
"""
AI Reviewer — Walk-Forward 파라미터 변경 분석 + 주간 성과 요약 (GitHub Copilot API 활용)

실행 흐름:
  1. TuningRun 최근 2건: 이전 vs 신규 파라미터 비교
  2. Orders 테이블 최근 7일: 실제 체결(buy/sell) 기반 성과 집계
     - 총 체결 건수, 승률, 총 PnL, 평균 ROI, 최대 수익/손실 트레이드
  3. GitHub Copilot gpt-4o-mini에게 헤지펀드 스타일 한국어 브리핑 생성 요청
  4. Telegram으로 전송

스케줄: 매주 일요일 04:30 (auto_tuner 04:00 완료 후)
"""
import os
import sys
import pathlib
import logging
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / 'trading_bot' / '.env', override=False)
except Exception:
    pass

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# 1. TuningRun 비교
# ---------------------------------------------------------------------------

def _fetch_tuning_runs(n=2):
    """최근 n개 TuningRun을 내림차순으로 반환. 없으면 빈 리스트."""
    from trading_bot.db import get_session
    from trading_bot.models import TuningRun
    session = get_session()
    try:
        rows = (
            session.query(TuningRun)
            .order_by(TuningRun.created_at.desc())
            .limit(n)
            .all()
        )
        return [
            {
                'combo': r.combo or {},
                'metrics': r.metrics or {},
                'created_at': r.created_at,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning('TuningRun 조회 실패: %s', e)
        return []
    finally:
        session.close()


def _build_param_diff(prev: dict, new: dict) -> list[str]:
    """두 combo dict 비교 → 변경된 파라미터 설명 목록 반환."""
    PARAM_LABELS = {
        'ema_short': 'EMA 단기',
        'ema_long': 'EMA 장기',
        'adx_trend_threshold': 'ADX 추세 임계값',
        'macro_ema_long': '일봉 Macro EMA 기간',
        'rsi_period': 'RSI 기간',
        'atr_period': 'ATR 기간',
    }
    diffs = []
    all_keys = set(prev) | set(new)
    for k in sorted(all_keys):
        label = PARAM_LABELS.get(k, k)
        pv = prev.get(k, 'N/A')
        nv = new.get(k, 'N/A')
        if pv != nv:
            diffs.append(f'  • {label}: {pv} → {nv}')
        else:
            diffs.append(f'  • {label}: {nv} (변경 없음)')
    return diffs


# ---------------------------------------------------------------------------
# 2. 주간 성과 집계 (Orders 테이블)
# ---------------------------------------------------------------------------

def _fetch_weekly_performance(days=7) -> dict:
    """
    Orders 테이블에서 최근 N일 체결 기록을 읽어 성과 지표 반환.
    raw JSON 구조: {side, price, qty, ticker, entry_price(sell만)}
    """
    from trading_bot.db import get_session
    from trading_bot.models import Order

    since = datetime.now(tz=KST) - timedelta(days=days)
    session = get_session()
    try:
        orders = (
            session.query(Order)
            .filter(Order.ts >= since, Order.status == 'filled')
            .order_by(Order.ts.asc())
            .all()
        )
    except Exception as e:
        logger.warning('Orders 조회 실패: %s', e)
        return {}
    finally:
        session.close()

    buys, sells = [], []
    for o in orders:
        raw = o.raw or {}
        side = o.side or raw.get('side', '')
        price = float(o.price or raw.get('price') or 0)
        qty = float(o.qty or raw.get('qty') or 0)
        ticker = raw.get('ticker', 'UNKNOWN')
        entry_price = float(raw.get('entry_price') or 0)

        if side == 'buy':
            buys.append({'ticker': ticker, 'price': price, 'qty': qty, 'ts': o.ts})
        elif side == 'sell' and price > 0 and qty > 0:
            pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price > 0 else None
            pnl_krw = (price - entry_price) * qty if entry_price > 0 else None
            sells.append({
                'ticker': ticker,
                'price': price,
                'entry_price': entry_price,
                'qty': qty,
                'pnl_pct': pnl_pct,
                'pnl_krw': pnl_krw,
                'ts': o.ts,
            })

    wins = [s for s in sells if s['pnl_pct'] is not None and s['pnl_pct'] > 0]
    losses = [s for s in sells if s['pnl_pct'] is not None and s['pnl_pct'] <= 0]
    total_pnl_krw = sum(s['pnl_krw'] for s in sells if s['pnl_krw'] is not None)
    avg_roi = (sum(s['pnl_pct'] for s in sells if s['pnl_pct'] is not None) / len(sells)) if sells else 0.0
    win_rate = (len(wins) / len(sells) * 100) if sells else 0.0

    best = max(sells, key=lambda x: x['pnl_pct'] or -999) if sells else None
    worst = min(sells, key=lambda x: x['pnl_pct'] or 999) if sells else None

    # 가장 많이 거래된 티커 Top 3
    from collections import Counter
    ticker_counts = Counter(o.get('ticker', '') for o in [*[{'ticker': b['ticker']} for b in buys], *[{'ticker': s['ticker']} for s in sells]])
    top_tickers = [t for t, _ in ticker_counts.most_common(3)]

    return {
        'period_days': days,
        'total_buys': len(buys),
        'total_sells': len(sells),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate_pct': round(win_rate, 1),
        'total_pnl_krw': round(total_pnl_krw, 0),
        'avg_roi_pct': round(avg_roi, 2),
        'best_trade': {'ticker': best['ticker'], 'pnl_pct': round(best['pnl_pct'], 2)} if best and best.get('pnl_pct') else None,
        'worst_trade': {'ticker': worst['ticker'], 'pnl_pct': round(worst['pnl_pct'], 2)} if worst and worst.get('pnl_pct') else None,
        'top_tickers': top_tickers,
    }


# ---------------------------------------------------------------------------
# 3. GitHub Copilot 브리핑 생성
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """당신은 퀀트 헤지펀드의 수석 전략 분석가입니다.
매주 트레이딩 봇의 파라미터 변경 내역과 실적을 검토하고,
전문적이고 간결한 한국어 주간 브리핑을 작성합니다.
브리핑은 다음 섹션으로 구성하세요:
1. 📊 파라미터 변경 분석 (변경 이유 및 시장 시사점 해석)
2. 📈 주간 성과 요약 (승률, PnL, 주요 거래)
3. 🔭 다음 주 전망 및 유의사항
텔레그램 메시지로 전송될 예정이므로, 반드시 1000자 이내로 핵심만 명확하게 요약하십시오."""


def _build_user_prompt(tuning_runs: list, perf: dict, param_diffs: list[str]) -> str:
    now_str = datetime.now(tz=KST).strftime('%Y년 %m월 %d일 %H:%M KST')

    # TuningRun 정보 구성
    if len(tuning_runs) >= 2:
        new_run, prev_run = tuning_runs[0], tuning_runs[1]
        new_fv = new_run['metrics'].get('final_value', 0)
        prev_fv = prev_run['metrics'].get('final_value', 0)
        fv_change = new_fv - prev_fv
        fv_sign = '+' if fv_change >= 0 else ''
        tuning_section = (
            f"[파라미터 튜닝 결과]\n"
            f"이전 combo ({prev_run['created_at'].strftime('%m/%d %H:%M')}): {prev_run['combo']}\n"
            f"신규 combo ({new_run['created_at'].strftime('%m/%d %H:%M')}): {new_run['combo']}\n"
            f"백테스트 final_value: {prev_fv:,.0f}원 → {new_fv:,.0f}원 ({fv_sign}{fv_change:,.0f}원)\n"
            f"\n변경 파라미터:\n" + '\n'.join(param_diffs)
        )
    elif len(tuning_runs) == 1:
        run = tuning_runs[0]
        tuning_section = (
            f"[파라미터 튜닝 결과]\n"
            f"현재 combo: {run['combo']}\n"
            f"백테스트 final_value: {run['metrics'].get('final_value', 0):,.0f}원\n"
            f"(이전 기록 없음 — 첫 튜닝)"
        )
    else:
        tuning_section = "[파라미터 튜닝 결과]\n데이터 없음"

    # 성과 섹션
    if perf:
        best_str = f"{perf['best_trade']['ticker']} +{perf['best_trade']['pnl_pct']}%" if perf.get('best_trade') else 'N/A'
        worst_str = f"{perf['worst_trade']['ticker']} {perf['worst_trade']['pnl_pct']}%" if perf.get('worst_trade') else 'N/A'
        perf_section = (
            f"\n[최근 {perf['period_days']}일 실거래 성과]\n"
            f"매수 체결: {perf['total_buys']}건 | 매도 체결: {perf['total_sells']}건\n"
            f"승률: {perf['win_rate_pct']}% ({perf['wins']}승 {perf['losses']}패)\n"
            f"총 PnL: {perf['total_pnl_krw']:+,.0f}원 | 평균 ROI: {perf['avg_roi_pct']:+.2f}%\n"
            f"최고 트레이드: {best_str}\n"
            f"최악 트레이드: {worst_str}\n"
            f"주요 거래 종목: {', '.join(perf['top_tickers']) if perf['top_tickers'] else 'N/A'}"
        )
    else:
        perf_section = "\n[최근 7일 실거래 성과]\n체결 데이터 없음"

    return (
        f"리포트 기준일: {now_str}\n\n"
        f"{tuning_section}\n"
        f"{perf_section}\n\n"
        "위 데이터를 바탕으로 주간 브리핑을 작성하세요."
    )


def _call_copilot(user_prompt: str) -> str:
    api_key = os.environ.get('GITHUB_TOKEN', '')
    if not api_key:
        raise EnvironmentError('GITHUB_TOKEN이 설정되지 않았습니다.')

    from openai import OpenAI
    client = OpenAI(
        base_url='https://models.inference.ai.azure.com',
        api_key=api_key,
    )
    response = client.chat.completions.create(
        model='gpt-4o-mini',
        max_tokens=600,
        messages=[
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': user_prompt},
        ],
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# 4. 메인 진입점
# ---------------------------------------------------------------------------

def run_ai_reviewer() -> None:
    """스케줄러에서 호출되는 진입점. 실패해도 예외를 삼켜 스케줄러를 보호."""
    logger.info('[AI Reviewer] 시작')
    try:
        _run()
    except Exception as e:
        logger.error('[AI Reviewer] 실패: %s', e, exc_info=True)
        # 실패 시 간단한 오류 알림만 전송
        try:
            from trading_bot.monitor import send_telegram
            send_telegram(f'⚠️ AI Reviewer 실행 실패: {e}')
        except Exception:
            pass


def _run() -> None:
    from trading_bot.monitor import send_telegram

    # 1) TuningRun 조회
    tuning_runs = _fetch_tuning_runs(n=2)

    # 2) 파라미터 diff
    if len(tuning_runs) >= 2:
        param_diffs = _build_param_diff(tuning_runs[1]['combo'], tuning_runs[0]['combo'])
    elif len(tuning_runs) == 1:
        param_diffs = [f'  • {k}: {v}' for k, v in tuning_runs[0]['combo'].items()]
    else:
        param_diffs = ['  • 데이터 없음']

    # 3) 주간 성과 집계
    perf = _fetch_weekly_performance(days=7)

    # 4) 프롬프트 구성 및 API 호출
    user_prompt = _build_user_prompt(tuning_runs, perf, param_diffs)
    logger.debug('[AI Reviewer] 프롬프트:\n%s', user_prompt)

    briefing = _call_copilot(user_prompt)
    logger.info('[AI Reviewer] 브리핑 생성 완료 (%d자)', len(briefing))

    # Telegram 4096자 제한 안전장치: 4000자 초과 시 잘라냄
    if len(briefing) > 4000:
        briefing = briefing[:4000] + '\n\n...(길이 제한으로 생략됨)'
        logger.warning('[AI Reviewer] 브리핑이 4000자를 초과하여 잘렸습니다.')

    # 5) Telegram 전송
    header = f'🤖 *AI 주간 트레이딩 리뷰* — {datetime.now(tz=KST).strftime("%m/%d %H:%M")}\n\n'
    send_telegram(header + briefing)
    logger.info('[AI Reviewer] Telegram 전송 완료')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    run_ai_reviewer()
