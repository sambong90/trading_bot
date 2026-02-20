from datetime import datetime
import time
from trading_bot.tasks.state_updater import update_phase

class PaperExecutor:
    def __init__(self, initial_cash=100000):
        self.cash = initial_cash
        self.position = 0.0
        self.log = []
        # executor stages
        self.stages = {
            'C.validation': {'weight':40, 'progress':0},
            'C.sim_fill': {'weight':30, 'progress':0},
            'C.retry': {'weight':20, 'progress':0},
            'C.logging': {'weight':10, 'progress':0}
        }
        update_phase('C - 실행기(Paper)', status='in_progress', stages=self.stages)

    def _update_stage(self, name, progress):
        # internal helper to update a stage progress
        if name in self.stages:
            self.stages[name]['progress'] = progress
        update_phase('C - 실행기(Paper)', status='in_progress', stages=self.stages)

    def _persist_order(self, rec, status='filled'):
        try:
            from trading_bot.db import get_session
            from trading_bot.models import Order
            import pandas as pd
            session = get_session()
            o = Order(order_id=str(int(pd.Timestamp.now().timestamp()*1000)), ts=pd.to_datetime(rec['time']).to_pydatetime(), side=rec['side'], price=rec['price'], qty=rec['qty'], status=status, fee=0.0, raw={})
            session.add(o)
            session.commit()
            session.close()
        except Exception as e:
            print('Failed to persist order:', e)

    def place_order(self, side, price, size_pct=1.0):
        # validation
        self._update_stage('C.validation', 20)
        time.sleep(0.01)
        if side == 'buy':
            qty = (self.cash * size_pct) / price
            cost = qty * price
            if cost <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['잘못된 주문금액'])
                return
            self._update_stage('C.sim_fill', 30)
            # simulate partial fills with simple model
            filled = qty
            self.position += filled
            self.cash -= filled * price
            rec = {'time': datetime.utcnow().isoformat(), 'side':'buy', 'price': price, 'qty': filled}
            self.log.append(rec)
            self._update_stage('C.sim_fill', 100)
            self._update_stage('C.logging', 100)
            # persist
            self._persist_order(rec, status='filled')
            update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'buy executed price={price} qty={filled:.6f}'], stages=self.stages)
        elif side == 'sell':
            self._update_stage('C.validation', 50)
            if self.position <= 0:
                update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=['sell skipped no position'])
                self._update_stage('C.validation', 100)
                return
            self._update_stage('C.sim_fill', 50)
            proceeds = self.position * price
            self.cash += proceeds
            rec = {'time': datetime.utcnow().isoformat(), 'side':'sell', 'price': price, 'qty': self.position}
            self.log.append(rec)
            self.position = 0.0
            self._update_stage('C.sim_fill', 100)
            self._update_stage('C.logging', 100)
            # persist
            self._persist_order(rec, status='filled')
            update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'sell executed price={price}'], stages=self.stages)

class LiveExecutor:
    ENABLE_AUTO_LIVE = None  # populated from env at init

    def _load_env_flags(self):
        import os
        self.ENABLE_AUTO_LIVE = os.environ.get('ENABLE_AUTO_LIVE') == '1'
        try:
            self.MAX_DAILY_LOSS_KRW = float(os.environ.get('MAX_DAILY_LOSS_KRW', '50000'))
        except Exception:
            self.MAX_DAILY_LOSS_KRW = 50000.0
        try:
            self.MAX_POSITION_PCT = float(os.environ.get('MAX_POSITION_PCT', '0.1'))
        except Exception:
            self.MAX_POSITION_PCT = 0.1
        self.TELEGRAM_ALERTS = os.environ.get('TELEGRAM_ALERTS','false').lower() in ('1','true','yes')

:
    def __init__(self, access_key=None, secret_key=None):
        # real implementation: requires UPBIT_ACCESS_KEY & UPBIT_SECRET_KEY in env
        import os
        self.access_key = access_key or os.environ.get('UPBIT_ACCESS_KEY')
        self.secret_key = secret_key or os.environ.get('UPBIT_SECRET_KEY')
        self.client = None
        self.enabled = False
        if self.access_key and self.secret_key and os.environ.get('LIVE_MODE') == '1' and os.environ.get('LIVE_CONFIRM') == 'I CONFIRM LIVE':
            try:
                import pyupbit
                self.client = pyupbit.Upbit(self.access_key, self.secret_key)
                self.enabled = True
            except Exception as e:
                print('LiveExecutor init failed:', e)

    def _persist_order(self, rec, status='submitted'):
        # persist live order to DB and log for auditing
        try:
            from trading_bot.db import get_session
            from trading_bot.models import Order
            import pandas as pd
            session = get_session()
            # create Order record if model available
            o = Order(order_id=str(rec.get('order_id') or int(pd.Timestamp.now().timestamp()*1000)), ts=pd.to_datetime(rec.get('time')).to_pydatetime(), side=rec.get('side'), price=rec.get('price'), qty=rec.get('qty') or 0.0, status=status, fee=0.0, raw=rec)
            session.add(o)
            session.commit()
            session.close()
        except Exception as e:
            print('Failed to persist live order:', e)

    def place_order(self, side, price, size_pct=1.0):
        import os, pandas as pd
        if not self.enabled:
            raise RuntimeError('LiveExecutor not enabled. Set LIVE_MODE=1 and LIVE_CONFIRM="I CONFIRM LIVE" and valid keys.')
        # basic validation
        if side not in ('buy','sell'):
            raise ValueError('side must be buy or sell')
        # get KRW balance for buys or asset balance for sells as needed
        # place limit order for safety
        try:
            # wrap network calls with simple retry/backoff
            import time
            max_retries = 3
            for attempt in range(1, max_retries+1):
                try:
                    if side == 'buy':
                        # find KRW balance from balances dict
                        bal_list = self.client.get_balances()
                        krw_bal = 0.0
                        for b in bal_list:
                            if b.get('currency') == 'KRW':
                                krw_bal = float(b.get('balance') or 0)
                                break
                        spend = krw_bal * size_pct
                        # daily loss guard
                        try:
                            if self._daily_loss_exceeded(additional_spend=spend):
                                raise RuntimeError('Daily loss limit exceeded, blocking new buys')
                        except Exception:
                            pass
                        ticker = 'KRW-AXS'
                        # ensure spend respects exchange min_total
                        try:
                            import requests
                            r = requests.get(f'https://api.upbit.com/v1/orders/chance?market={ticker}', timeout=5)
                            data = r.json()
                            min_total = float(data.get('market', {}).get('bid', {}).get('min_total') or data.get('market', {}).get('ask', {}).get('min_total') or 1000)
                        except Exception:
                            # if chance endpoint requires auth, fall back to conservative default
                            min_total = 5000
                        if spend < min_total:
                            spend = min_total
                        # buy_limit_order expects volume (qty), not spend — compute qty = spend / price
                        qty = spend / price if price else 0
                        resp = self.client.buy_limit_order(ticker, price, qty)
                    else:
                        ticker = 'KRW-AXS'
                        # for sell, we need the asset balance
                        bal_list = self.client.get_balances()
                        asset_bal = 0.0
                        for b in bal_list:
                            if b.get('currency') == ticker.split('-')[1]:
                                asset_bal = float(b.get('balance') or 0)
                                break
                        sell_qty = asset_bal * size_pct if size_pct <= 1 else size_pct
                        resp = self.client.sell_market_order(ticker, sell_qty)
                    # parse response: pyupbit returns dict with 'uuid' or similar
                    order_id = None
                    if isinstance(resp, dict):
                        order_id = resp.get('uuid') or resp.get('id') or resp.get('uuid')
                    rec = {'time': pd.Timestamp.now().isoformat(), 'side': side, 'price': price, 'qty': spend if side=='buy' else resp}
                    # persist order with order_id if available
                    try:
                        self._persist_order({**rec, 'order_id': order_id}, status='submitted')
                    except Exception:
                        pass
                    return resp
                except Exception as e:
                    print(f'Live order attempt {attempt} failed:', e)
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise
        except Exception as e:
            print('Live order failed:', e)
            raise

    def _notify_telegram(self, text):
        try:
            from trading_bot.monitor import send_telegram
            if getattr(self,'TELEGRAM_ALERTS',False):
                send_telegram(text)
        except Exception:
            pass

    def _reload_env_flags(self):
        import os
        try:
            self.ENABLE_AUTO_LIVE = os.environ.get('ENABLE_AUTO_LIVE') == '1'
            self.MAX_DAILY_LOSS_KRW = float(os.environ.get('MAX_DAILY_LOSS_KRW', self.MAX_DAILY_LOSS_KRW))
            self.MAX_POSITION_PCT = float(os.environ.get('MAX_POSITION_PCT', self.MAX_POSITION_PCT))
            self.TELEGRAM_ALERTS = os.environ.get('TELEGRAM_ALERTS','true').lower() in ('1','true','yes')
        except Exception:
            pass

    def _start_env_watcher(self):
        # spawn a background thread to reload flags periodically so panic endpoint takes effect without restart
        try:
            import threading, time
            def _loop():
                while True:
                    try:
                        self._reload_env_flags()
                    except Exception:
                        pass
                    time.sleep(5)
            t=threading.Thread(target=_loop, daemon=True)
            t.start()
        except Exception:
            pass


    def _daily_loss_exceeded(self, additional_spend=0.0):
        # compute realized P&L for today using trades table (sells add KRW, buys subtract KRW), include fees
        try:
            from datetime import datetime, timedelta
            from trading_bot.db import get_session
            from trading_bot.models import Trade
            session=get_session()
            today = datetime.utcnow().date()
            start_dt = datetime(today.year, today.month, today.day)
            trades = session.query(Trade).filter(Trade.ts >= start_dt).all()
            pnl = 0.0
            fees = 0.0
            for t in trades:
                try:
                    side = (t.side or '').lower()
                    price = float(t.price or 0)
                    qty = float(t.qty or 0)
                    fee = float(t.fee or 0)
                    if side == 'sell':
                        pnl += price * qty
                    elif side == 'buy':
                        pnl -= price * qty
                    fees += fee
                except Exception:
                    pass
            session.close()
            net = pnl - fees
            # include additional planned spend as further loss
            net -= float(additional_spend or 0)
            # add unrealized P&L: estimate current value of holdings in KRW
            try:
                import pyupbit
                balances = self.client.get_balances() if hasattr(self,'client') and self.client else []
                unreal = 0.0
                for b in balances:
                    try:
                        cur_bal = float(b.get('balance') or 0)
                        if cur_bal <= 0:
                            continue
                        currency = b.get('currency')
                        if currency == 'KRW':
                            unreal += cur_bal
                            continue
                        market = f'KRW-{currency}'
                        price = pyupbit.get_current_price(market)
                        if price is None:
                            continue
                        unreal += cur_bal * float(price)
                    except Exception:
                        pass
                # include unrealized current value into net estimate
                net += unreal
            except Exception:
                pass
            # if net loss beyond threshold, return True
            return net < -float(getattr(self,'MAX_DAILY_LOSS_KRW',50000))
        except Exception:
            return False

