import ccxt
import pandas as pd
import time
import numpy as np
import os
from datetime import datetime

# ==========================================
# 1. 系統初始化與全局變數
# ==========================================
exchange = ccxt.bybit({'enableRateLimit': True})
LOG_DIR = "result_short"
LOG_FILE = f"{LOG_DIR}/09_short_log.csv"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

# 全局帳戶與狀態追蹤
initial_balance = 10000.0
balance = initial_balance
total_fees_paid = 0.0
positions = {}
cooldown_tracker = {}  # 冷卻期紀錄字典：{symbol: {'loss_count': int, 'cooldown_until': float}}

# 策略核心參數 (做空大池專用)
FEE_RATE = 0.00055
NET_FLOW_SIGMA = 1.5  # 捕捉踩踏：淨流出需大於 1.5 倍標準差
TP_ATR_MULT = 1.5  # 止盈：1.5x ATR
SL_ATR_MULT = 1.0  # 初始止損：1.0x ATR
TRAIL_ATR_MULT = 1.0  # 追蹤止損：1.0x ATR (確保快速插水行情能拿住利潤)
RISK_PER_TRADE = 0.01  # 單筆風險 1%
MAX_LEVERAGE = 1.0  # 槓桿封頂 1.0x，防止注碼失控

# 統一 CSV 數據庫欄位 (解決格式混亂、計錯數的終極方案)
CSV_COLUMNS = [
    'timestamp', 'symbol', 'action', 'price', 'amount',
    'trade_value', 'atr', 'net_flow', 'tp_price', 'sl_price',
    'reason', 'realized_pnl', 'fee_paid', 'balance'
]


# ==========================================
# 2. 輔助與日誌功能
# ==========================================
def log_to_csv(data_dict):
    """標準化日誌寫入，確保每一行都有相同數量的 Column，絕不錯位"""
    # 建立一個全空的標準字典
    row = {col: '' for col in CSV_COLUMNS}
    # 將傳入的數據覆蓋進去
    row.update(data_dict)
    # 補上時間戳記與當前餘額
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row['balance'] = round(balance, 2)

    df = pd.DataFrame([row], columns=CSV_COLUMNS)
    file_exists = os.path.exists(LOG_FILE)
    df.to_csv(LOG_FILE, mode='a', index=False, header=not file_exists)


def get_market_metrics(symbol):
    """計算波動率 ATR，過濾死水市場"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=20)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
        atr = df['tr'].rolling(14).mean().iloc[-1]
        is_volatile = (atr / df['c'].iloc[-1]) > 0.0005
        return atr, is_volatile
    except:
        return None, False


# ==========================================
# 3. 海選與微觀信號 (捕捉弱勢崩潰)
# ==========================================
def scouting_weak_coins(n=5):
    """【做空海選】尋找 Top 50 內大戶恐慌砸盤，跌破支撐的弱勢幣"""
    try:
        tickers = exchange.fetch_tickers()
        data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
                for s, t in tickers.items() if s.endswith(':USDT') and t['percentage'] is not None]
        df = pd.DataFrame(data)
        if df.empty: return []

        # 1. 篩選成交量 Top 50 保證流動性
        # 2. 按 Change % 由小到大排序 (即最負的排前面，跌得最勁)
        weak_coins = df.sort_values('volume', ascending=False).head(50) \
            .sort_values('change', ascending=True).head(n)['symbol'].tolist()
        return weak_coins
    except Exception as e:
        print(f"海選錯誤: {e}")
        return []


def apply_lee_ready_logic(symbol):
    """Lee-Ready 判定：計算微觀資金淨流向"""
    try:
        ob = exchange.fetch_order_book(symbol)
        midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
        trades = exchange.fetch_trades(symbol, limit=200)

        df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
        df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
        df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
        df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])

        flow = df['final'] * df['amount']
        net_flow = flow.sum()

        # 強勢空頭信號：淨流出大於 1.5 倍標準差
        is_strong = net_flow < -(flow.std() * NET_FLOW_SIGMA)
        return net_flow, df['price'].iloc[-1], is_strong
    except:
        return 0, 0, False


# ==========================================
# 4. 做空交易執行 (進場)
# ==========================================
def execute_sim_short(symbol, net_flow, price, is_strong, atr, is_volatile):
    global balance, total_fees_paid
    current_time = time.time()

    # 【保命符】檢查冷卻期
    if symbol in cooldown_tracker:
        if current_time < cooldown_tracker[symbol].get('cooldown_until', 0):
            return

    if is_strong and is_volatile and symbol not in positions:
        current_v = sum(info['amount'] * price for s, info in positions.items())
        equity = balance + current_v

        # 動態注碼計算 (Volatility Scaling)
        risk_amount = equity * RISK_PER_TRADE
        trade_value = risk_amount / (atr / price)

        # 1.0x 槓桿封頂
        trade_value = min(trade_value, equity * MAX_LEVERAGE, balance * 0.95)
        if trade_value < 10: return

        fee = trade_value * FEE_RATE
        balance -= (trade_value + fee)
        total_fees_paid += fee

        amount = trade_value / price
        tp_price = price - (TP_ATR_MULT * atr)
        sl_price = price + (SL_ATR_MULT * atr)

        positions[symbol] = {
            'amount': amount, 'entry_price': price,
            'tp_price': tp_price, 'sl_price': sl_price,
            'is_breakeven': False, 'atr': atr
        }

        # 完整寫入對齊的 CSV
        log_to_csv({
            'symbol': symbol, 'action': 'SHORT_ENTRY', 'price': price,
            'amount': amount, 'trade_value': round(trade_value, 2), 'atr': round(atr, 4),
            'net_flow': round(net_flow, 2), 'tp_price': round(tp_price, 4),
            'sl_price': round(sl_price, 4), 'fee_paid': round(fee, 4)
        })
        print(f"📉 [做空進場] {symbol} | 價格: {price:.4f} | 注碼: {trade_value:.2f} USDT")


# ==========================================
# 5. 持倉管理與冷卻機制 (退場)
# ==========================================
def manage_short_positions():
    global balance, total_fees_paid
    for s in list(positions.keys()):
        try:
            curr_p = exchange.fetch_ticker(s)['last']
            pos = positions[s]
            pnl_pct = (pos['entry_price'] - curr_p) / pos['entry_price']

            # 保本鎖定
            if not pos['is_breakeven'] and pnl_pct > (FEE_RATE * 2):
                pos['sl_price'] = pos['entry_price'] * 0.9998
                pos['is_breakeven'] = True
                print(f"🛡️ {s} 空頭已啟動保本")

            # 追蹤止損 (1.0x ATR)
            if pos['is_breakeven']:
                new_sl = curr_p + (TRAIL_ATR_MULT * pos['atr'])
                if new_sl < pos['sl_price']:
                    pos['sl_price'] = new_sl

            reason = None
            if curr_p <= pos['tp_price']:
                reason = "Take Profit"
            elif curr_p >= pos['sl_price']:
                reason = "Stop Loss/Trail"

            if reason:
                amount = pos['amount']
                pnl = (pos['entry_price'] - curr_p) * amount
                trade_value = (amount * pos['entry_price']) + pnl
                exit_fee = trade_value * FEE_RATE

                balance += (trade_value - exit_fee)
                total_fees_paid += exit_fee

                # 【保命符】連續虧損冷卻機制
                if pnl < 0:
                    cooldown_tracker.setdefault(s, {'loss_count': 0, 'cooldown_until': 0})
                    cooldown_tracker[s]['loss_count'] += 1

                    if cooldown_tracker[s]['loss_count'] >= 2:
                        cooldown_tracker[s]['cooldown_until'] = time.time() + 1800  # 30分鐘
                        print(f"❄️ [保命符觸發] {s} 連續止損兩次，禁賽 30 分鐘！")
                else:
                    cooldown_tracker[s] = {'loss_count': 0, 'cooldown_until': 0}

                # 完整寫入對齊的 CSV
                log_to_csv({
                    'symbol': s, 'action': 'SHORT_COVER', 'price': curr_p,
                    'amount': amount, 'reason': reason, 'realized_pnl': round(pnl, 4),
                    'fee_paid': round(exit_fee, 4)
                })
                del positions[s]
                print(f"✅ [平倉退場] {s} | 原因: {reason} | PnL: {pnl:.2f} USDT")
        except Exception as e:
            print(f"管理持倉錯誤 {s}: {e}")


# ==========================================
# 6. 獨立績效結算模組
# ==========================================
def calculate_performance():
    """從標準化 CSV 讀取數據，進行嚴謹的績效結算"""
    if not os.path.exists(LOG_FILE):
        return

    try:
        df = pd.read_csv(LOG_FILE)
        covers = df[df['action'] == 'SHORT_COVER'].copy()

        if covers.empty:
            return

        # 強制轉型，避免計錯數
        covers['realized_pnl'] = pd.to_numeric(covers['realized_pnl'], errors='coerce').fillna(0)

        total_trades = len(covers)
        winning_trades = len(covers[covers['realized_pnl'] > 0])
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = covers['realized_pnl'].sum()

        print("\n" + "=" * 45)
        print("📈 【策略績效統計 (Short Pool)】 📈")
        print(f"總平倉次數: {total_trades} 次")
        print(f"勝率 (Win Rate): {win_rate:.2f}%")
        print(f"累積實現盈虧: {total_pnl:.2f} USDT")
        print(f"當前帳戶餘額: {balance:.2f} USDT")
        print("=" * 45 + "\n")
    except Exception as e:
        print(f"⚠️ 績效計算讀取失敗: {e}")


# ==========================================
# 7. 主程式循環
# ==========================================
def main():
    print(f"🚀 AI 空頭大池 2.0 (專業風控完全體) 啟動...")
    print(f"📁 數據將嚴格對齊儲存至: {LOG_FILE}")

    cycle_count = 0
    while True:
        try:
            manage_short_positions()

            # 抓出跌最勁的 5 隻幣
            target_coins = scouting_weak_coins(5)

            for s in target_coins:
                flow, last_p, is_strong = apply_lee_ready_logic(s)
                atr, is_volatile = get_market_metrics(s)
                if last_p > 0:
                    execute_sim_short(s, flow, last_p, is_strong, atr, is_volatile)

            # Dashboard 與績效計算
            cycle_count += 1
            if cycle_count % 5 == 0:  # 每跑 5 個循環 (約 10 分鐘) 印出一次完整績效
                calculate_performance()
            else:
                total_v = sum(info['amount'] * info['entry_price'] for s, info in positions.items())
                print(f"⏳ 監控中... Equity: {balance + total_v:.2f} | 餘額: {balance:.2f} | 持倉數: {len(positions)}")

        except Exception as e:
            print(f"⚠️ 主迴圈錯誤: {e}")
        time.sleep(120)


if __name__ == "__main__":
    main()