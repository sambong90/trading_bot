
from flask import Flask, render_template, jsonify, send_file, abort
import pandas as pd
import plotly.express as px
import plotly.io as pio
import json
import pathlib

LOG_DIR = pathlib.Path(__file__).parent / 'logs'
app = Flask(__name__, template_folder='templates')

def load_results():
    try:
        df = pd.read_csv(LOG_DIR / 'risk_backtest_results.csv')
        return df
    except Exception:
        return pd.DataFrame()

@app.route('/')
def index():
    df = load_results()
    charts = {}
    if not df.empty:
        figs = []
        figs.append(px.bar(df, x='short', y='final_value', color='long', barmode='group', title='Final Value by Params'))
        figs.append(px.bar(df, x='short', y='n_trades', color='long', barmode='group', title='Trades by Params'))
        for i,fig in enumerate(figs):
            charts[f'chart{i}'] = pio.to_html(fig, full_html=False, include_plotlyjs='cdn')
    return render_template('index.html', charts=charts, df=df.to_dict(orient='records'))

@app.route('/combo/<int:short>-<int:long>')
def combo_detail(short, long):
    # show equity curve and trade log for a specific combo
    # try to load per-combo detailed logs in logs/combo_{short}_{long}.json (if exists)
    combo_file = LOG_DIR / f'combo_{short}_{long}.json'
    if not combo_file.exists():
        abort(404, description='Combo detail not found. Run backtests with detailed logging.')
    data = json.loads(combo_file.read_text())
    # data expected: {'equity': [{'time':..., 'value':...}, ...], 'trades':[...], 'metrics':{...}}
    equity_df = pd.DataFrame(data.get('equity', []))
    fig_equity = None
    if not equity_df.empty:
        fig_equity = pio.to_html(px.line(equity_df, x='time', y='value', title=f'Equity Curve {short}/{long}'), full_html=False, include_plotlyjs='cdn')
    return render_template('combo.html', equity_html=fig_equity, trades=data.get('trades', []), metrics=data.get('metrics', {}), short=short, long=long)

@app.route('/download/<path:filename>')
def download_file(filename):
    fn = LOG_DIR / filename
    if not fn.exists():
        abort(404)
    return send_file(fn)

@app.route('/data')
def data_api():
    try:
        df = pd.read_csv(LOG_DIR / 'risk_backtest_results.csv')
        return df.to_json(orient='records')
    except Exception as e:
        return jsonify({'error':str(e)})

if __name__ == '__main__':
    app.run(port=5000)

