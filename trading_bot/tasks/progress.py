
import json
import time
from pathlib import Path
LOG_PATH = Path('trading_bot/logs/progress.json')
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

def update_progress(phase, task, percent=0, msg=None):
    now = time.time()
    entry = {
        'ts': now,
        'phase': phase,
        'task': task,
        'percent': percent,
        'msg': msg
    }
    # write latest state
    try:
        with open(LOG_PATH, 'w') as f:
            json.dump(entry, f)
    except Exception:
        pass

def read_progress():
    try:
        with open(LOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {'ts': time.time(), 'phase': 'idle', 'task': None, 'percent': 0, 'msg': None}

