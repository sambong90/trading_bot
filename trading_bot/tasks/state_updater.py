#!/usr/bin/env python3
import json
import pathlib
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATUS_FILE = ROOT / 'logs' / 'current_phase.json'
STATUS_FILE.parent.mkdir(exist_ok=True)


def update_phase(phase: str, status: str = 'in_progress', percent: int = None, recent_actions=None, next_steps=None, tests=None, issues=None, stages=None, auto_percent: bool = True):
    """Update current_phase.json.
    stages: optional dict. Two supported shapes:
      - simple: {name: progress_int}
      - weighted: {name: {'weight':int,'progress':int}}
    If auto_percent is True and stages provided, percent is computed as weighted average of stage progresses.
    """
    stages = stages or {}
    computed = None
    if auto_percent and stages:
        try:
            # detect weighted format
            weighted = False
            total_weight = 0.0
            acc = 0.0
            simple_vals = []
            for k,v in stages.items():
                if isinstance(v, dict) and 'progress' in v and 'weight' in v:
                    weighted = True
                    w = float(v.get('weight', 0))
                    p = float(v.get('progress', 0))
                    total_weight += w
                    acc += w * p
                else:
                    # assume simple number
                    try:
                        p = float(v)
                        simple_vals.append(p)
                    except Exception:
                        pass
            if weighted and total_weight>0:
                computed = int(acc/total_weight)
            elif simple_vals:
                computed = int(sum(simple_vals)/len(simple_vals))
            else:
                computed = 0
        except Exception:
            computed = 0
        percent = computed
    if percent is None:
        percent = 0

    obj = {
        'phase': phase,
        'status': status,
        'percent': percent,
        'stages': stages,
        'recent_actions': recent_actions or [],
        'next_steps': next_steps or [],
        'tests': tests or {},
        'issues': issues or [],
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')
    }
    STATUS_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    return obj


if __name__ == '__main__':
    print(update_phase('예시 단계', 'in_progress', None, ['작업1 완료'], ['다음 작업'], stages={'B.equity':{'weight':40,'progress':50},'B.metrics':{'weight':60,'progress':20}}))

