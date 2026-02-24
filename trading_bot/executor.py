from datetime import datetime
import time
from trading_bot.tasks.state_updater import update_phase

class PaperExecutor:
    def __init__(self, initial_cash=100000):
        self.cash = initial_cash
        # 티커별 포지션: { ticker: {'qty': float, 'avg_price': float}, ... }
        self.positions = {}
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
            o = Order(order_id=str(int(pd.Timestamp.now().timestamp()*1000)), ts=pd.to_datetime(rec['time']).to_pydatetime(), side=rec['side'], price=rec['price'], qty=rec['qty'], status=status, fee=0.0, raw=rec)
            session.add(o)
            session.commit()
            session.close()
        except Exception as e:
            print('Failed to persist order:', e)

    def place_order(self, side, price, size_pct=1.0, ticker='KRW-BTC'):
        """
        Paper 모드 주문. 티커별 포지션(qty, 평균단가)을 독립 관리.
        LiveExecutor와 시그니처 동일: side, price, size_pct, ticker.
        """
        # validation
        self._update_stage('C.validation', 20)
        time.sleep(0.01)
        if side == 'buy':
            qty = (self.cash * size_pct) / price if price else 0
            cost = qty * price
            if cost <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['잘못된 주문금액'])
                return
            self._update_stage('C.sim_fill', 30)
            filled = qty
            self.cash -= filled * price
            # 티커별 포지션 갱신 (평균 단가)
            if ticker not in self.positions:
                self.positions[ticker] = {'qty': 0.0, 'avg_price': 0.0}
            prev = self.positions[ticker]
            total_qty = prev['qty'] + filled
            prev['avg_price'] = (prev['qty'] * prev['avg_price'] + filled * price) / total_qty if total_qty else 0
            prev['qty'] = total_qty
            rec = {'time': datetime.utcnow().isoformat(), 'side': 'buy', 'price': price, 'qty': filled, 'ticker': ticker}
            self.log.append(rec)
            self._update_stage('C.sim_fill', 100)
            self._update_stage('C.logging', 100)
            self._persist_order(rec, status='filled')
            update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} buy executed price={price} qty={filled:.6f}'], stages=self.stages)
        elif side == 'sell':
            self._update_stage('C.validation', 50)
            pos = self.positions.get(ticker, {'qty': 0.0, 'avg_price': 0.0})
            hold_qty = pos['qty']
            if hold_qty <= 0:
                update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} sell skipped no position'])
                self._update_stage('C.validation', 100)
                return
            self._update_stage('C.sim_fill', 50)
            sell_qty = hold_qty * size_pct if size_pct <= 1 else min(size_pct, hold_qty)
            proceeds = sell_qty * price
            self.cash += proceeds
            rec = {'time': datetime.utcnow().isoformat(), 'side': 'sell', 'price': price, 'qty': sell_qty, 'ticker': ticker}
            self.log.append(rec)
            pos['qty'] -= sell_qty
            if pos['qty'] <= 0:
                del self.positions[ticker]
            self._update_stage('C.sim_fill', 100)
            self._update_stage('C.logging', 100)
            self._persist_order(rec, status='filled')
            update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} sell executed price={price} qty={sell_qty:.6f}'], stages=self.stages)

    def refresh_balance_cache(self):
        """Paper: 로컬 positions 사용으로 캐시 갱신 불필요 (no-op)."""
        pass

    def get_position_qty(self, ticker):
        """해당 티커 보유 수량. Paper는 로컬 positions 기준."""
        return float(self.positions.get(ticker, {}).get('qty', 0) or 0)

    def get_avg_buy_price(self, ticker):
        """해당 티커 평균 매수가. Paper는 로컬 positions 기준."""
        return float(self.positions.get(ticker, {}).get('avg_price', 0) or 0)

    def get_available_cash(self):
        """가용 현금(KRW). Two-Pass Pass 2 매수 한도 판단용."""
        return float(self.cash)

    def get_cash(self):
        """가용 현금(KRW). get_available_cash()와 동일. 호출부 호환용."""
        return float(self.cash)

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

    def __init__(self, access_key=None, secret_key=None):
        # real implementation: requires UPBIT_ACCESS_KEY & UPBIT_SECRET_KEY in env
        import os
        self.access_key = access_key or os.environ.get('UPBIT_ACCESS_KEY')
        self.secret_key = secret_key or os.environ.get('UPBIT_SECRET_KEY')
        self.client = None
        self.enabled = False
        
        # 환경 변수 플래그 로드
        self._load_env_flags()
        
        self._balance_cache = {}  # 사이클당 1회 갱신하여 get_position_qty 시 API 비용 절감
        if self.access_key and self.secret_key and os.environ.get('LIVE_MODE') == '1' and os.environ.get('LIVE_CONFIRM') == 'I CONFIRM LIVE':
            try:
                import pyupbit
                self.client = pyupbit.Upbit(self.access_key, self.secret_key)
                self.enabled = True
                # 환경 변수 감시 스레드 시작
                self._start_env_watcher()
                print('LiveExecutor initialized successfully')
            except Exception as e:
                print(f'LiveExecutor init failed: {e}')
                self.enabled = False

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

    def place_order(self, side, price, size_pct=1.0, ticker='KRW-BTC'):
        """
        실제 거래 주문 실행 (개선된 버전)
        
        Parameters:
        - side: 'buy' or 'sell'
        - price: 주문 가격
        - size_pct: 포지션 크기 비율
        - ticker: 거래할 코인 티커 (하드코딩 제거)
        """
        import os, pandas as pd
        if not self.enabled:
            raise RuntimeError('LiveExecutor not enabled. Set LIVE_MODE=1 and LIVE_CONFIRM="I CONFIRM LIVE" and valid keys.')
        
        # basic validation
        if side not in ('buy','sell'):
            raise ValueError('side must be buy or sell')
        
        if not ticker or not ticker.startswith('KRW-'):
            raise ValueError(f'Invalid ticker: {ticker}. Must be in format KRW-XXX')
        
        # 환경 변수 로드
        self._load_env_flags()
        
        # ENABLE_AUTO_LIVE 플래그 확인 (실제 주문 실행 여부)
        if not self.ENABLE_AUTO_LIVE:
            raise RuntimeError('ENABLE_AUTO_LIVE=0 - 자동 라이브 거래가 비활성화되어 있습니다. 실제 주문을 실행하지 않습니다.')
        
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
                        
                        # 포지션 크기 제한 확인 (spend = 사용할 원화 총액)
                        spend_float = krw_bal * min(size_pct, self.MAX_POSITION_PCT)
                        
                        # daily loss guard
                        try:
                            if self._daily_loss_exceeded(additional_spend=spend_float):
                                raise RuntimeError('Daily loss limit exceeded, blocking new buys')
                        except Exception:
                            pass
                        
                        # 최소 주문 금액 확인 (업비트 최소 주문 typically 5000 KRW)
                        try:
                            import requests
                            r = requests.get(f'https://api.upbit.com/v1/orders/chance?market={ticker}', timeout=5)
                            data = r.json()
                            min_total = float(data.get('market', {}).get('bid', {}).get('min_total') or 
                                            data.get('market', {}).get('ask', {}).get('min_total') or 1000)
                        except Exception:
                            min_total = 5000
                        
                        if spend_float < min_total:
                            raise ValueError(f'주문 금액이 최소 주문 금액({min_total}원)보다 작습니다.')
                        
                        # 시장가 매수: 업비트 API는 원화 총액(spend)만 받음. 지정가 미체결 방지.
                        spend = round(spend_float)  # KRW 정수 원 단위
                        resp = self.client.buy_market_order(ticker, spend)
                        
                        # Telegram 알림 (시장가이므로 체결가 대신 주문 원화 표시)
                        self._notify_telegram(f'🟢 시장가 매수: {ticker}, 주문 금액: {spend:,.0f}원')
                        
                    else:
                        # for sell, we need the asset balance
                        bal_list = self.client.get_balances()
                        asset_bal = 0.0
                        asset_currency = ticker.split('-')[1]
                        
                        for b in bal_list:
                            if b.get('currency') == asset_currency:
                                asset_bal = float(b.get('balance') or 0)
                                break
                        
                        if asset_bal <= 0:
                            raise ValueError(f'{asset_currency} 잔고가 없습니다.')
                        
                        sell_qty = asset_bal * size_pct if size_pct <= 1 else size_pct
                        if sell_qty <= 0:
                            raise ValueError('매도 수량이 0입니다.')
                        
                        resp = self.client.sell_market_order(ticker, sell_qty)
                        
                        # Telegram 알림
                        self._notify_telegram(f'🔴 매도 주문: {ticker}, 수량: {sell_qty:.6f}')
                    
                    # parse response: pyupbit returns dict with 'uuid' or similar
                    order_id = None
                    if isinstance(resp, dict):
                        order_id = resp.get('uuid') or resp.get('id') or resp.get('uuid')
                    
                    # buy: 시장가라 체결가/수량은 응답 기준. 로깅용으로 price=참고가, qty=예상 수량
                    rec = {
                        'time': pd.Timestamp.now().isoformat(), 
                        'side': side, 
                        'price': price, 
                        'qty': (spend / price) if (side == 'buy' and price) else sell_qty,
                        'ticker': ticker
                    }
                    if side == 'buy':
                        rec['spend'] = spend  # 시장가 매수 시 실제 주문 원화
                    
                    # persist order with order_id if available
                    try:
                        self._persist_order({**rec, 'order_id': order_id}, status='submitted')
                    except Exception:
                        pass
                    
                    return resp
                except Exception as e:
                    error_msg = f'Live order attempt {attempt} failed: {e}'
                    print(error_msg)
                    self._notify_telegram(f'⚠️ {error_msg}')
                    
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise
        except Exception as e:
            error_msg = f'Live order failed: {e}'
            print(error_msg)
            self._notify_telegram(f'❌ {error_msg}')
            raise

    def refresh_balance_cache(self):
        """사이클 시작 시 1회 호출. get_balances()로 잔고·평균매수가 캐시 갱신."""
        if not getattr(self, 'client', None):
            return
        try:
            bal_list = self.client.get_balances()
            self._balance_cache = {}
            self._avg_buy_price_cache = {}
            for b in bal_list:
                cur = b.get('currency')
                if cur:
                    self._balance_cache[cur] = float(b.get('balance') or 0)
                    try:
                        avg = b.get('avg_buy_price')
                        self._avg_buy_price_cache[cur] = float(avg) if avg is not None else 0.0
                    except (TypeError, ValueError):
                        self._avg_buy_price_cache[cur] = 0.0
        except Exception:
            self._balance_cache = getattr(self, '_balance_cache', {})
            self._avg_buy_price_cache = getattr(self, '_avg_buy_price_cache', {})

    def get_position_qty(self, ticker):
        """해당 티커 보유 수량. refresh_balance_cache() 호출 후 사용 권장 (Live 시 API 1회만)."""
        asset = ticker.split('-')[1] if '-' in ticker else ''
        return float(self._balance_cache.get(asset, 0) or 0)

    def get_avg_buy_price(self, ticker):
        """해당 티커 평균 매수가. refresh_balance_cache() 호출 후 사용 권장."""
        asset = ticker.split('-')[1] if '-' in ticker else ''
        return float(getattr(self, '_avg_buy_price_cache', {}).get(asset, 0) or 0)

    def get_available_cash(self):
        """가용 현금(KRW). Two-Pass Pass 2 매수 한도 판단용. refresh_balance_cache() 후 호출 권장."""
        return float(self._balance_cache.get('KRW', 0) or 0)

    def get_cash(self):
        """
        업비트 계좌 원화(KRW) 가용 잔고 반환.
        get_balances()에서 KRW 추출, API 실패 또는 None 시 0.0 반환.
        """
        try:
            if not getattr(self, 'client', None):
                return 0.0
            bal_list = self.client.get_balances()
            if not bal_list:
                return 0.0
            for b in bal_list:
                if b.get('currency') == 'KRW':
                    v = b.get('balance')
                    return float(v) if v is not None else 0.0
            return 0.0
        except Exception:
            return 0.0

    def _notify_telegram(self, text):
        try:
            from trading_bot.monitor import send_telegram
            import logging
            logger = logging.getLogger(__name__)
            if getattr(self,'TELEGRAM_ALERTS',False):
                success, error_msg = send_telegram(text)
                if not success:
                    logger.warning(f'⚠️ 텔레그램 전송 실패 (Executor): {error_msg or "알 수 없는 오류"}')
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f'⚠️ 텔레그램 전송 중 오류 (Executor): {e}', exc_info=True)

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


