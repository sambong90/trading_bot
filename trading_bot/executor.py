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
            if side == 'buy':
                krw_bal = float(self.client.get_balances()[0]['balance'])
                # compute size
                spend = krw_bal * size_pct
                resp = self.client.buy_limit_order('KRW-BTC', price, spend)
            else:
                # for sell, use market sell for full position (simplified)
                resp = self.client.sell_market_order('KRW-BTC', size_pct)
            # persist order
            rec = {'time': pd.Timestamp.now().isoformat(), 'side': side, 'price': price, 'qty': resp}
            self._persist_order(rec, status=str(resp))
            return resp
        except Exception as e:
            print('Live order failed:', e)
            raise
