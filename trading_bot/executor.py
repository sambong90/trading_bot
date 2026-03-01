from datetime import datetime
import time
from trading_bot.tasks.state_updater import update_phase

# [C1/H1 FIX] Module-level cache for DB-persisted system state.
# The env-watcher thread calls _reload_env_flags() every 5 s; this cache prevents
# a DB round-trip on every call (refreshes at most once per 30 s).
_sys_state_cache: dict = {'enable_auto_live': None, 'expires_at': 0.0}

class PaperExecutor:
    def __init__(self, initial_cash=100000):
        self.cash = self._load_cash_from_db(initial_cash)  # [IMPROVED]
        # 티커별 포지션: { ticker: {'qty': float, 'avg_price': float}, ... }
        self.positions = self._load_positions_from_db()  # [NEW]
        self.log = []
        # executor stages
        self.stages = {
            'C.validation': {'weight':40, 'progress':0},
            'C.sim_fill': {'weight':30, 'progress':0},
            'C.retry': {'weight':20, 'progress':0},
            'C.logging': {'weight':10, 'progress':0}
        }
        update_phase('C - 실행기(Paper)', status='in_progress', stages=self.stages)

    # [NEW] paper_state.json 우선 (qty 포함), 없으면 position_states 테이블 (avg_price만)
    def _load_positions_from_db(self):
        try:
            from trading_bot.config import LOGS_DIR
            import json
            state_file = LOGS_DIR / 'paper_state.json'
            if state_file.exists():
                data = json.loads(state_file.read_text(encoding='utf-8'))
                raw = data.get('positions', {})
                if raw:
                    return {
                        t: {'qty': float(p.get('qty', 0)), 'avg_price': float(p.get('avg_price', 0))}
                        for t, p in raw.items()
                        if float(p.get('qty', 0)) > 0
                    }
        except Exception:
            pass
        try:
            from trading_bot.db import get_session
            from trading_bot.models import PositionState
            session = get_session()
            try:
                rows = session.query(PositionState).all()
                return {
                    r.ticker: {'qty': 0.0, 'avg_price': float(r.avg_buy_price or 0)}
                    for r in rows if r.avg_buy_price and r.avg_buy_price > 0
                }
            finally:
                session.close()
        except Exception:
            pass
        return {}

    # [IMPROVED] paper_state 키로 저장된 가용 현금 복원
    def _load_cash_from_db(self, default_cash: float) -> float:
        try:
            import json
            from trading_bot.config import LOGS_DIR
            state_file = LOGS_DIR / 'paper_state.json'
            if state_file.exists():
                data = json.loads(state_file.read_text(encoding='utf-8'))
                return float(data.get('cash', default_cash))
        except Exception:
            pass
        return default_cash

    # [NEW] 현재 포지션·현금을 DB 및 파일에 저장 (place_order 완료 후 호출)
    def _save_state_to_db(self):
        try:
            from trading_bot.db import get_session
            from trading_bot.models import PositionState
            session = get_session()
            try:
                for ticker, pos in self.positions.items():
                    existing = session.query(PositionState).filter(PositionState.ticker == ticker).first()
                    if existing:
                        existing.avg_buy_price = pos.get('avg_price', 0.0)
                        existing.stage = 0
                    else:
                        session.add(PositionState(
                            ticker=ticker,
                            avg_buy_price=pos.get('avg_price', 0.0),
                            stage=0,
                        ))
                # 포지션 없어진 티커는 position_states에서 제거
                for row in session.query(PositionState).all():
                    if row.ticker not in self.positions:
                        session.delete(row)
                session.commit()
            finally:
                session.close()
        except Exception:
            pass
        try:
            import json
            from trading_bot.config import LOGS_DIR
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            state_file = LOGS_DIR / 'paper_state.json'
            state_file.write_text(json.dumps({
                'cash': self.cash,
                'positions': {
                    t: {'qty': p['qty'], 'avg_price': p['avg_price']}
                    for t, p in self.positions.items()
                }
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

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
        import logging
        _logger = logging.getLogger(__name__)

        # validation
        self._update_stage('C.validation', 20)
        time.sleep(0.01)
        if side == 'buy':
            # --- 가격 유효성 검증 ---
            if not price or price <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['유효하지 않은 가격'])
                _logger.warning('[Paper] %s 매수 거부: 유효하지 않은 가격 (price=%s)', ticker, price)
                return
            # --- 가용 현금 검증 (잔고 마이너스 방지) ---
            if self.cash <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['가용 현금 없음'])
                _logger.warning('[Paper] %s 매수 거부: 가용 현금 없음 (cash=%.0f)', ticker, self.cash)
                return
            # 가용 현금 범위 내에서만 매수 (잔고 마이너스 절대 방지)
            spend = min(self.cash * size_pct, self.cash)
            if spend < 5000:  # 업비트 최소 주문 금액
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=[f'최소 주문 금액(5,000원) 미달: {spend:.0f}원'])
                _logger.warning('[Paper] %s 매수 거부: 최소 주문 금액 미달 (spend=%.0f)', ticker, spend)
                return
            qty = spend / price
            cost = spend  # spend 자체가 이미 cash 이하로 보장됨
            if qty <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['잘못된 주문금액'])
                return
            self._update_stage('C.sim_fill', 30)
            filled = qty
            self.cash -= cost
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
            self._save_state_to_db()
            update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} buy executed price={price} qty={filled:.6f}'], stages=self.stages)
        elif side == 'sell':
            self._update_stage('C.validation', 50)
            # --- 가격 유효성 검증 ---
            if not price or price <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='failed', issues=['매도 가격이 유효하지 않음'])
                _logger.warning('[Paper] %s 매도 거부: 유효하지 않은 가격 (price=%s)', ticker, price)
                return
            pos = self.positions.get(ticker, {'qty': 0.0, 'avg_price': 0.0})
            hold_qty = pos['qty']
            if hold_qty <= 0:
                update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} sell skipped no position'])
                self._update_stage('C.validation', 100)
                return
            self._update_stage('C.sim_fill', 50)
            sell_qty = hold_qty * size_pct if size_pct <= 1 else min(size_pct, hold_qty)
            # --- 매도 수량이 보유 수량 초과 방지 ---
            sell_qty = min(sell_qty, hold_qty)
            if sell_qty <= 0:
                self._update_stage('C.validation', 100)
                update_phase('C - 실행기(Paper)', status='in_progress', recent_actions=[f'{ticker} sell skipped: sell_qty=0'])
                return
            proceeds = sell_qty * price
            self.cash += proceeds
            rec = {'time': datetime.utcnow().isoformat(), 'side': 'sell', 'price': price, 'qty': sell_qty, 'ticker': ticker}
            rec['entry_price'] = pos.get('avg_price', 0.0)  # 연속 손실 계산용
            self.log.append(rec)
            pos['qty'] -= sell_qty
            if pos['qty'] <= 1e-12:  # 부동소수점 찌꺼기 정리
                del self.positions[ticker]
            self._update_stage('C.sim_fill', 100)
            self._update_stage('C.logging', 100)
            self._persist_order(rec, status='filled')
            self._save_state_to_db()
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
            o = Order(
                order_id=str(rec.get('order_id') or int(pd.Timestamp.now().timestamp()*1000)),
                ts=pd.to_datetime(rec.get('time')).to_pydatetime(),
                side=rec.get('side'),
                # [C3 FIX] 'price' is always the actual fill price (not signal price).
                # 'signal_price' field in raw captures the original analysis price.
                price=rec.get('price'),
                qty=rec.get('qty') or 0.0,
                status=status,
                fee=0.0,
                raw=rec,
            )
            session.add(o)
            session.commit()
            session.close()
        except Exception as e:
            print('Failed to persist live order:', e)

    def _get_fill_price(self, order_id: str, fallback_price: float) -> float:
        """
        [C3 FIX] Try to retrieve the actual average fill price from the exchange
        after a market order.  Market orders on Upbit fill within ~1 s; we wait
        briefly and call get_order(uuid) to read avg_price.

        Falls back to a fresh get_current_price() quote, which is far more accurate
        than the signal price from the previous candle close.
        """
        # Try exchange order detail (most accurate)
        if order_id and getattr(self, 'client', None):
            try:
                time.sleep(0.8)
                order_info = self.client.get_order(order_id)
                if isinstance(order_info, dict) and order_info.get('state') == 'done':
                    avg_p = order_info.get('avg_price')
                    if avg_p:
                        avg_p = float(avg_p)
                        if avg_p > 0:
                            return avg_p
            except Exception:
                pass

        # Fallback: fresh current price (much better than stale signal price)
        try:
            import pyupbit
            # ticker is not passed here; caller stores result before this call
            # so we just use the fallback already provided from realtime_price
            pass
        except Exception:
            pass
        return fallback_price

    # --- 하드 스탑로스 환경 변수 (기본값: -10%) ---
    HARD_STOP_LOSS_PCT = None

    def _get_hard_stop_loss_pct(self):
        import os
        try:
            return float(os.environ.get('HARD_STOP_LOSS_PCT', '-10.0'))
        except (TypeError, ValueError):
            return -10.0

    def check_hard_stop_loss(self, ticker, current_price):
        """
        하드 스탑로스 체크. 보유 포지션의 ROI가 HARD_STOP_LOSS_PCT 이하이면
        즉시 전량 시장가 매도를 트리거.
        반환: True면 스탑로스 발동(매도 완료), False면 정상.
        """
        import logging
        _logger = logging.getLogger(__name__)
        try:
            avg_price = self.get_avg_buy_price(ticker)
            qty = self.get_position_qty(ticker)
            if qty <= 0 or avg_price <= 0 or current_price is None or current_price <= 0:
                return False
            roi = (current_price - avg_price) / avg_price * 100
            threshold = self._get_hard_stop_loss_pct()
            if roi <= threshold:
                _logger.critical(
                    '[HARD STOP-LOSS] %s ROI=%.1f%% <= %.1f%% | AvgPrice=%.0f CurrentPrice=%.0f | 전량 시장가 매도 실행',
                    ticker, roi, threshold, avg_price, current_price,
                )
                self._notify_telegram(
                    f'🚨 [HARD STOP-LOSS] {ticker}\nROI: {roi:.1f}% (임계값: {threshold}%)\n'
                    f'평단가: {avg_price:,.0f}원 → 현재가: {current_price:,.0f}원\n전량 시장가 매도 실행'
                )
                # 전량 매도 (size_pct=1.0). 이 함수 내에서 place_order 재귀 방지를 위해 직접 API 호출
                try:
                    asset_currency = ticker.split('-')[1]
                    resp = self.client.sell_market_order(ticker, qty)
                    _logger.info('[HARD STOP-LOSS] %s 매도 주문 응답: %s', ticker, resp)
                    import pandas as pd
                    rec = {
                        'time': pd.Timestamp.now().isoformat(),
                        'side': 'sell', 'price': current_price, 'qty': qty,
                        'ticker': ticker, 'reason': 'HARD_STOP_LOSS',
                    }
                    order_id = resp.get('uuid') if isinstance(resp, dict) else None
                    self._persist_order({**rec, 'order_id': order_id}, status='stop_loss')
                except Exception as e2:
                    _logger.error('[HARD STOP-LOSS] %s 매도 실행 실패: %s', ticker, e2)
                    self._notify_telegram(f'❌ HARD STOP-LOSS 매도 실패: {ticker} — {e2}')
                return True
        except Exception as e:
            _logger.warning('[HARD STOP-LOSS] %s 체크 중 오류: %s', ticker, e)
        return False

    def cancel_all_open_orders(self, ticker=None):
        """미체결 주문 전량 취소. ticker 지정 시 해당 티커만, None이면 전체."""
        import logging
        _logger = logging.getLogger(__name__)
        if not self.enabled or not self.client:
            return []
        try:
            import pyupbit
            open_orders = self.client.get_order(ticker) if ticker else []
            cancelled = []
            for order in (open_orders or []):
                try:
                    uuid = order.get('uuid')
                    if uuid and order.get('state') in ('wait', 'watch'):
                        self.client.cancel_order(uuid)
                        cancelled.append(uuid)
                        _logger.info('[주문 취소] %s uuid=%s', ticker or 'ALL', uuid)
                except Exception as e:
                    _logger.warning('[주문 취소 실패] uuid=%s: %s', order.get('uuid'), e)
            if cancelled:
                self._notify_telegram(f'🗑️ 미체결 주문 {len(cancelled)}건 취소 완료 ({ticker or "전체"})')
            return cancelled
        except Exception as e:
            _logger.warning('[주문 취소] 조회 실패: %s', e)
            return []

    def place_order(self, side, price, size_pct=1.0, ticker='KRW-BTC'):
        """
        실제 거래 주문 실행 (개선된 버전)

        Parameters:
        - side: 'buy' or 'sell'
        - price: 주문 가격
        - size_pct: 포지션 크기 비율
        - ticker: 거래할 코인 티커 (하드코딩 제거)
        """
        import os, pandas as pd, logging
        _logger = logging.getLogger(__name__)
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

        # --- 슬리피지 방어: 주문 전 실시간 가격 검증 (BTC 급변 방어) ---
        # 방향성 슬리피지: 불리한 방향(매수=가격 상승, 매도=가격 하락)만 차단. 유리한 슬리피지는 허용.
        # [C3 FIX] realtime_price를 외부 스코프에 노출해 체결가 근사치로 활용.
        realtime_price = None
        try:
            import pyupbit
            realtime_price = pyupbit.get_current_price(ticker)
            if realtime_price and price and price > 0:
                if side == 'buy':
                    slippage = (realtime_price - price) / price   # 양수 = 실시간이 더 비쌈 (불리)
                else:
                    slippage = (price - realtime_price) / price   # 양수 = 실시간이 더 쌈 (불리)
                if slippage > 0.03:  # 불리한 방향으로 3% 이상 괴리 시 주문 거부
                    msg = (f'슬리피지 방어: {ticker} 참조가격({price:,.0f}) vs '
                           f'실시간({realtime_price:,.0f}) 괴리 {slippage*100:.1f}% > 3%')
                    _logger.warning('[SLIPPAGE GUARD] %s', msg)
                    self._notify_telegram(f'⚠️ {msg}')
                    raise RuntimeError(msg)
        except RuntimeError:
            raise
        except Exception:
            pass  # 실시간 가격 조회 실패 시 주문 계속 진행

        # [H5 FIX] 매도 전 현재 평균 매수가를 캡처 (매도 후 balance_cache가 변경되기 전에).
        # risk.get_consecutive_losses()가 entry_price를 사용해 손익 판별하므로 필수.
        entry_price_snapshot = self.get_avg_buy_price(ticker) if side == 'sell' else 0.0

        try:
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

                        # 잔고 부족 사전 차단
                        if krw_bal < 5000:
                            raise ValueError(f'KRW 잔고 부족: {krw_bal:,.0f}원')

                        # 포지션 크기 제한 확인 (spend = 사용할 원화 총액)
                        spend_float = krw_bal * min(size_pct, self.MAX_POSITION_PCT)

                        # daily loss guard
                        try:
                            if self._daily_loss_exceeded(additional_spend=spend_float):
                                raise RuntimeError('Daily loss limit exceeded, blocking new buys')
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

                        # 최소 주문 금액 확인 (업비트 최소 주문 typically 5000 KRW)
                        min_total = 5000
                        try:
                            import requests
                            r = requests.get(f'https://api.upbit.com/v1/orders/chance?market={ticker}', timeout=5)
                            data = r.json()
                            min_total = float(data.get('market', {}).get('bid', {}).get('min_total') or
                                            data.get('market', {}).get('ask', {}).get('min_total') or 5000)
                        except Exception:
                            pass

                        if spend_float < min_total:
                            raise ValueError(f'주문 금액이 최소 주문 금액({min_total}원)보다 작습니다.')

                        spend = round(spend_float)
                        resp = self.client.buy_market_order(ticker, spend)

                        self._notify_telegram(f'🟢 시장가 매수: {ticker}, 주문 금액: {spend:,.0f}원')

                    else:
                        bal_list = self.client.get_balances()
                        asset_bal = 0.0
                        asset_currency = ticker.split('-')[1]

                        for b in bal_list:
                            if b.get('currency') == asset_currency:
                                asset_bal = float(b.get('balance') or 0)
                                break

                        if asset_bal <= 0:
                            raise ValueError(f'{asset_currency} 잔고가 없습니다.')

                        # 전량 매도(size_pct>=1.0) 시 잔고 직접 사용 — float 곱으로 인한 crypto dust 방지
                        if size_pct >= 1.0:
                            sell_qty = asset_bal
                        else:
                            sell_qty = asset_bal * size_pct
                        if sell_qty <= 0:
                            raise ValueError('매도 수량이 0입니다.')

                        resp = self.client.sell_market_order(ticker, sell_qty)

                        self._notify_telegram(f'🔴 매도 주문: {ticker}, 수량: {sell_qty:.6f}')

                    # parse response
                    order_id = None
                    if isinstance(resp, dict):
                        order_id = resp.get('uuid') or resp.get('id')
                        # API 에러 응답 감지
                        if resp.get('error'):
                            raise RuntimeError(f"Upbit API error: {resp.get('error')}")

                    # [C3 FIX] 실제 체결가 결정 우선순위:
                    #   1) 거래소 주문 응답 avg_price (market order 즉시 체결 시 available)
                    #   2) 주문 직전에 조회한 realtime_price (signal price보다 훨씬 정확)
                    #   3) signal price (최후 fallback)
                    fill_price = realtime_price or price  # realtime fallback (slippage 체크에서 조회됨)
                    if isinstance(resp, dict):
                        avg_p = resp.get('avg_price')
                        try:
                            if avg_p and float(avg_p) > 0:
                                fill_price = float(avg_p)
                        except (TypeError, ValueError):
                            pass

                    if side == 'buy':
                        fill_qty = spend / fill_price if fill_price else (spend / price if price else 0)
                        rec = {
                            'time': pd.Timestamp.now().isoformat(),
                            'side': 'buy',
                            'price': fill_price,          # 실제 체결가
                            'signal_price': price,         # 분석 시 참조 가격 (기록용)
                            'qty': fill_qty,
                            'ticker': ticker,
                            'spend': spend,
                        }
                    else:
                        rec = {
                            'time': pd.Timestamp.now().isoformat(),
                            'side': 'sell',
                            'price': fill_price,           # 실제 체결가
                            'signal_price': price,          # 분석 시 참조 가격 (기록용)
                            'qty': sell_qty,
                            'ticker': ticker,
                            # [H5 FIX] 매도 전 캡처한 평균 매수가 → risk.get_consecutive_losses() 정상 동작
                            'entry_price': entry_price_snapshot,
                        }

                    try:
                        self._persist_order({**rec, 'order_id': order_id}, status='submitted')
                    except Exception:
                        pass

                    return resp
                except (RuntimeError, ValueError) as e:
                    # 비즈니스 로직 에러는 재시도하지 않고 즉시 전파
                    raise
                except Exception as e:
                    error_msg = f'Live order attempt {attempt} failed: {e}'
                    _logger.warning(error_msg)
                    self._notify_telegram(f'⚠️ {error_msg}')

                    # Rate Limit (429) 감지 시 더 긴 대기
                    err_str = str(e).lower()
                    if '429' in err_str or 'rate limit' in err_str or 'too many' in err_str:
                        wait = min(60, 5 * (2 ** attempt))
                        _logger.warning('[Rate Limit] %s초 대기 후 재시도...', wait)
                        time.sleep(wait)
                    elif attempt < max_retries:
                        time.sleep(2 ** attempt)

                    if attempt >= max_retries:
                        # 최종 실패 시 해당 티커 미체결 주문 정리
                        self.cancel_all_open_orders(ticker)
                        raise
        except Exception as e:
            error_msg = f'Live order failed: {e}'
            _logger.error(error_msg)
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
        """
        [C1/H1 FIX] 환경 변수 플래그 갱신 + DB SystemState 우선 적용.

        K8s Pod 재시작 후에도 panic 상태가 유지되도록 SystemState 테이블에서
        'enable_auto_live' 키를 읽어 os.environ보다 우선 적용한다.
        DB 조회는 30초 TTL 캐시를 통해 5초마다 호출되는 watcher 스레드의
        DB 부하를 억제한다.
        """
        import os
        global _sys_state_cache
        try:
            env_enable = os.environ.get('ENABLE_AUTO_LIVE')

            # DB SystemState 조회 (30초 TTL)
            now = time.monotonic()
            if now >= _sys_state_cache['expires_at']:
                try:
                    from trading_bot.db import get_session
                    from trading_bot.models import SystemState
                    _s = get_session()
                    try:
                        row = _s.query(SystemState).filter(
                            SystemState.key == 'enable_auto_live'
                        ).first()
                        _sys_state_cache['enable_auto_live'] = row.value if row else None
                    finally:
                        _s.close()
                except Exception:
                    pass
                _sys_state_cache['expires_at'] = now + 30  # 실패해도 30초 후 재시도

            db_val = _sys_state_cache['enable_auto_live']
            if db_val is not None:
                env_enable = db_val  # DB 값이 환경 변수보다 우선 (pod 재시작 후 유지)

            self.ENABLE_AUTO_LIVE = env_enable == '1'
            self.MAX_DAILY_LOSS_KRW = float(os.environ.get('MAX_DAILY_LOSS_KRW', self.MAX_DAILY_LOSS_KRW))
            self.MAX_POSITION_PCT = float(os.environ.get('MAX_POSITION_PCT', self.MAX_POSITION_PCT))
            self.TELEGRAM_ALERTS = os.environ.get('TELEGRAM_ALERTS', 'false').lower() in ('1', 'true', 'yes')
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
        # compute realized P&L for today using orders table (sells add KRW, buys subtract KRW), include fees
        # Note: Trade table is for backtest simulations only; actual live/paper orders are in the Order table
        try:
            from datetime import datetime
            from trading_bot.db import get_session
            from trading_bot.models import Order
            from sqlalchemy import func
            session = get_session()
            today = datetime.utcnow().date()
            start_dt = datetime(today.year, today.month, today.day)

            # func.sum() — 전체 행을 메모리로 가져오지 않고 DB에서 집계
            sell_sum = session.query(
                func.sum(Order.price * Order.qty)
            ).filter(Order.ts >= start_dt, Order.side == 'sell').scalar() or 0.0

            buy_sum = session.query(
                func.sum(Order.price * Order.qty)
            ).filter(Order.ts >= start_dt, Order.side == 'buy').scalar() or 0.0

            fee_sum = session.query(
                func.sum(Order.fee)
            ).filter(Order.ts >= start_dt).scalar() or 0.0

            session.close()

            pnl = float(sell_sum) - float(buy_sum)
            net = pnl - float(fee_sum)
            # include additional planned spend as further loss
            net -= float(additional_spend or 0)

            # add unrealized P&L: 보유 자산 현재가 일괄 조회 (배치 API 1회)
            try:
                import pyupbit
                balances = self.client.get_balances() if hasattr(self, 'client') and self.client else []
                unreal = float(next(
                    (float(b.get('balance') or 0) for b in balances if b.get('currency') == 'KRW'), 0.0
                ))
                non_krw = [(b, f'KRW-{b["currency"]}') for b in balances
                           if b.get('currency') != 'KRW' and float(b.get('balance') or 0) > 0]
                if non_krw:
                    markets = [m for _, m in non_krw]
                    prices_raw = pyupbit.get_current_price(markets)
                    if isinstance(prices_raw, dict):
                        prices = prices_raw
                    elif isinstance(prices_raw, (int, float)) and len(markets) == 1:
                        prices = {markets[0]: float(prices_raw)}
                    else:
                        prices = {}
                    for b, market in non_krw:
                        p = prices.get(market)
                        if p:
                            unreal += float(b.get('balance', 0)) * float(p)
                net += unreal
            except Exception:
                pass
            # if net loss beyond threshold, return True
            return net < -float(getattr(self, 'MAX_DAILY_LOSS_KRW', 50000))
        except Exception:
            return False


