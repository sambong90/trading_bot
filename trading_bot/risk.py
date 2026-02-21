def check_daily_loss(cash, starting_cash, daily_loss_limit_pct=0.03):
    draw = (starting_cash - cash) / starting_cash
    return draw <= daily_loss_limit_pct

def check_total_drawdown(current_value, peak_value, max_drawdown_pct=0.15):
    draw = (peak_value - current_value) / peak_value
    return draw <= max_drawdown_pct


def calculate_adjusted_position_size(
    account_value,
    risk_per_trade_pct=0.02,
    stop_loss_pct=0.05,
    use_dynamic_adjustment=True,
):
    """포지션 크기(KRW) 및 리스크 조정 정보 반환. (strategy 호환)"""
    base_size = account_value * risk_per_trade_pct / (stop_loss_pct or 0.05)
    risk_adjustments = {
        'position_size_multiplier': 1.0,
        'is_defensive_mode': False,
        'consecutive_losses': 0,
        'win_rate': 0.0,
        'atr_trailing_multiplier': 2.0,
    }
    return base_size, risk_adjustments
