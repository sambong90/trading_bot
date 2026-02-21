#!/usr/bin/env python3
import os
import sys
import json
import pathlib
from datetime import datetime
# ensure workspace root on path so "from trading_bot..." works when run as script
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

# load env
BASE = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(BASE / '.env')

from trading_bot.monitor import send_telegram

LOG_DIR = BASE / 'logs'
STATUS_FILE = LOG_DIR / 'current_phase.json'


def read_phase():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return None
    return None


def _skip_risk_filter_line(s: str) -> bool:
    """리스크 필터 실패 등 정상 필터 메시지는 텔레그램/요약에서 제외"""
    if not s:
        return True
    return '리스크 필터 실패' in s or '필터 실패' in s


def human_card(phase):
    # Korean pretty card with stages
    lines = []
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')
    lines.append(f"📌 업데이트: {ts}")
    lines.append(f"단계: {phase.get('phase','알수없음')}  |  상태: {phase.get('status','') }  |  전체 진행률: {phase.get('percent',0)}%")
    # stages
    stages = phase.get('stages') or {}
    if stages:
        lines.append('\n🔧 세부 스테이지:')
        # support weighted format
        for k, v in stages.items():
            if isinstance(v, dict):
                w = v.get('weight', '?')
                p = v.get('progress', '?')
                lines.append(f"• {k}: {p}% (weight:{w})")
            else:
                lines.append(f"• {k}: {v}%")
    if phase.get('recent_actions'):
        lines.append('\n✅ 최근 완료')
        for a in phase.get('recent_actions')[:5]:
            if _skip_risk_filter_line(str(a)):
                continue
            lines.append(f"• {a}")
    if phase.get('next_steps'):
        lines.append('\n🔜 다음 작업')
        for n in phase.get('next_steps')[:3]:
            lines.append(f"• {n}")
    if phase.get('tests'):
        t = phase.get('tests')
        test_lines = '  '.join([f"{k}:{v}" for k,v in t.items()])
        lines.append(f"\n🧪 테스트: {test_lines}")
    if phase.get('issues'):
        lines.append('\n⚠️ 이슈')
        for it in phase.get('issues'):
            if _skip_risk_filter_line(str(it)):
                continue
            lines.append(f"• {it}")
    # include recent log preview (리스크 필터 실패 등 정상 필터 로그 제외)
    log_preview = []
    try:
        logs = sorted(LOG_DIR.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            with logs[0].open() as f:
                last = f.readlines()[-5:]
                for l in last:
                    line = l.strip()
                    if not line or _skip_risk_filter_line(line):
                        continue
                    log_preview.append(line)
    except Exception:
        log_preview = []
    if log_preview:
        lines.append('\n📄 최근 로그')
        for l in log_preview:
            lines.append(f"• {l}")
    return '\n'.join(lines)


def build_summary():
    phase = read_phase() or {}
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')
    files = []
    if LOG_DIR.exists():
        for p in LOG_DIR.glob('*'):
            files.append(p.name)
    summary = {
        'timestamp': ts,
        'phase': phase,
        'logs': files
    }
    return summary


def main():
    s = build_summary()
    phase = s['phase'] or {'phase':'unknown','status':'idle','percent':0}
    text = human_card(phase)
    # save structured summary
    out = LOG_DIR / 'summary_auto.json'
    out.write_text(json.dumps(s, indent=2))
    # also save human readable
    out2 = LOG_DIR / 'summary_auto.txt'
    out2.write_text(text)
    # send telegram if configured
    try:
        ok, _ = send_telegram(text)
        if not ok:
            print('Telegram send failed')
    except Exception as e:
        print('Telegram not configured or send failed:', e)
    print(text)


if __name__ == '__main__':
    main()
