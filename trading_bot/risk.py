def check_daily_loss(cash, starting_cash, daily_loss_limit_pct=0.03):
    draw = (starting_cash - cash) / starting_cash
    return draw <= daily_loss_limit_pct

def check_total_drawdown(current_value, peak_value, max_drawdown_pct=0.15):
    draw = (peak_value - current_value) / peak_value
    return draw <= max_drawdown_pct
