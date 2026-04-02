import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
from datetime import datetime

# ==========================================
# 0. 系統與日誌設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Short_V6.0')

# ⚠️ 請確保 API 金鑰正確
API_KEY = "xd8NcfedvibG9tP4iD"
API_SECRET = "ZzGICmYtkDHyTWgT1UiGpiesjz9b26Mactbw"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.load_markets()

LOG_DIR = "core/result_short_live"
LOG_FILE = f"{LOG_DIR}/09_live_short_log.csv"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

positions, cooldown_tracker = {}, {}

# 👑 策略參數 (做空版)
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0
SCOUTING_INTERVAL = 120
POSITION_CHECK_INTERVAL = 10
MIN_NOTIONAL = 5.0

BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT', 'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT', 'USDP/USDT:USDT', 'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT', 'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT', 'RLUSD/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT', 'WAVAX/USDT:USDT',
    'stETH/USDT:USDT', 'cbETH/USDT:USDT', 'WHT/USDT:USDT'
]

CSV_COLUMNS = ['timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value', 'atr', 'net_flow', 'tp_price',
               'sl_price', 'reason', 'realized_pnl', 'actual_balance', 'effective_balance']


# ==========================================
# 1. 核心輔助模組
# ==========================================
def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS};
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(LOG_FILE, mode='a', index=False,
                                                    header=not os.path.exists(LOG_FILE))


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    try:
        exchange.cancel_all_orders(symbol, params={'orderFilter': 'Order'})
        exchange.cancel_all_orders(symbol, params={'orderFilter': 'StopOrder'})
    except:
        pass


def get_market_metrics(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
        atr = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
        if pd.isna(atr) or atr == 0: return None, False
        return atr, (atr / df['c'].iloc[-1]) > 0.0005
    except:
        return None, False


def get_3_layer_avg_price(symbol, side='bids'):
    # 做空進場看買盤 (Bids)，出場看賣盤 (Asks)
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([level[0] for level in levels]) / len(levels)
    except:
        return None


# ==========================================
# 2. 導航與海選 (Short Side 邏輯翻轉 - Target Price 版)
# ==========================================
def get_btc_regime():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        sma50 = df['c'].rolling(50).mean().iloc[-1]

        # 🚀 核心門檻
        target_short = sma20 * (1 - 0.0025)
        deviation = (curr_p - sma20) / sma20

        # 📊 條件檢查
        cond_price = curr_p < target_short  # 價格是否夠低
        cond_trend = sma20 < sma50  # 趨勢是否轉空

        # 轉化為圖標
        tick_p = "✅" if cond_price else "❌"
        tick_t = "✅" if cond_trend else "❌"

        # 🚦 燈號邏輯
        if cond_price and cond_trend:
            status, signal = "🔴 紅燈 (空頭全軍出擊)", -1
        elif cond_price or cond_trend:
            status, signal = "🟡 黃燈 (條件未齊 - 觀望)", 0
        else:
            status, signal = "🟢 綠燈 (多頭強勢 - 撤退)", 1

        # 🚀 終極視覺化 Presentation
        print("-" * 60)
        print(f"📊 BTC 實時戰報 | 現價: {curr_p:.0f}")
        print(f"1️⃣ 價格門檻: {curr_p:.0f} < {target_short:.0f} {tick_p}")
        print(f"2️⃣ 趨勢確認: SMA20({sma20:.0f}) < SMA50({sma50:.0f}) {tick_t}")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return signal
    except Exception as e:
        print(f"⚠️ 導航故障: {e}")
        return 0


def scouting_weak_coins(n=5):
    """【海選】在大幣池中找跌勢最猛、表現最弱的標的"""
    try:
        tickers = exchange.fetch_tickers()
        data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
                for s, t in tickers.items() if
                s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None]
        df = pd.DataFrame(data)
        # 先選前 20 大成交量，再選漲幅最小 (即跌幅最大) 的 n 隻
        return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=True).head(n)[
            'symbol'].tolist()
    except:
        return []


def apply_lee_ready_short_logic(symbol):
    try:
        ob = exchange.fetch_order_book(symbol)
        midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
        trades = exchange.fetch_trades(symbol, limit=200)
        df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
        df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
        df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
        df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])
        # 金額加權流向
        df['weighted_flow'] = df['final'] * df['amount'] * df['price']
        net_flow = df['weighted_flow'].sum()
        # 做空訊號：淨流量為顯著負數
        is_weak = net_flow < -(df['weighted_flow'].std() * NET_FLOW_SIGMA)
        return net_flow, df['price'].iloc[-1], is_weak
    except:
        return 0, 0, False


# ==========================================
# 3. 持倉管理 (修正 2, 3：空單版)
# ==========================================
def manage_short_positions():
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 清理幽靈空單倉位: {s}")
                if s in cooldown_tracker: del cooldown_tracker[s]
                del positions[s];
                continue

        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            # 做空盈虧：(進場價 - 現價)
            pnl_pct = (pos['entry_price'] - curr_p) / pos['entry_price']
            sl_updated = False

            # 保本位 (獲利 > 0.3%，止損壓到成本下方一點點)
            if not pos['is_breakeven'] and pnl_pct > 0.003:
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 0.9998, True, True

            # 追蹤止損 (做空版：價格越低，止損往下推)
            if pos['is_breakeven']:
                trail_sl = curr_p + (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl < pos['sl_price']:  # 止損線往下壓才是有效追蹤
                    pos['sl_price'], sl_updated = trail_sl, True

            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop(
                        {'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                         'tpslMode': 'Full', 'positionIdx': 0})
                except:
                    pass

            exit_reason = None
            if curr_p <= pos['tp_price']:
                exit_reason = "TP (Short IOC Exit)"
            elif curr_p >= pos['sl_price'] and not pos['is_breakeven']:
                exit_reason = "SL (Short IOC Exit)"

            if exit_reason:
                print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平空單: {s}")
                try:
                    # 平空單要買回，看賣盤 (Asks)
                    ioc_price = get_3_layer_avg_price(s, 'asks') or curr_p
                    exchange.create_order(s, 'limit', 'buy', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_buy_order(s, pos['amount'], {'reduceOnly': True})

                log_to_csv({'symbol': s, 'action': 'SHORT_EXIT', 'price': curr_p, 'amount': pos['amount'],
                            'reason': exit_reason,
                            'realized_pnl': round((pos['entry_price'] - curr_p) * pos['amount'], 4)})
                cancel_all_v5(s)
                if s in cooldown_tracker: del cooldown_tracker[s]
                del positions[s]
    except Exception as e:
        if "10006" in str(e): time.sleep(5)


# ==========================================
# 4. 執行入場 (三段式開空單)
# ==========================================
def execute_live_short(symbol, net_flow, current_price, is_weak, atr, is_volatile):
    if symbol in cooldown_tracker and time.time() < cooldown_tracker[symbol]: return
    if not (is_weak and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)
    trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']: return
    # 進場開空，掛買盤 (Bids)
    ioc_p = get_3_layer_avg_price(symbol, 'bids') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        # 第一步：開空倉 (Sell)
        order = exchange.create_order(symbol, 'limit', 'sell', amount, ioc_p, {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        # 🛠️ 第二步：安全地獲取實際成交價 (修復 Bybit IOC 報錯問題)
        actual_price = ioc_p
        actual_amount = 0

        try:
            # 加入 params={"acknowledged": True} 嘗試解決 Bybit 警告
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 獲取訂單失敗，啟動備用持倉同步: {e}")
            time.sleep(0.5)
            # 如果 API 報錯，直接去查交易所目前的真實倉位！
            live_pos = exchange.fetch_positions()
            for p in live_pos:
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price = float(p.get('entryPrice') or ioc_p)
                    break

        # 如果最終確認數量是 0，代表真的沒成交 (IOC 取消了)
        if actual_amount == 0:
            print(f"⏩ {symbol} IOC 未成交，撤退。")
            return

        # 第三步：計算並物理設置止盈止損 (空單版)
        tp_p = float(exchange.price_to_precision(symbol, actual_price - (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price + (SL_ATR_MULT * atr)))

        try:
            exchange.private_post_v5_position_trading_stop(
                {'category': 'linear', 'symbol': exchange.market_id(symbol), 'stopLoss': str(sl_p),
                 'takeProfit': str(tp_p), 'tpslMode': 'Full', 'positionIdx': 0})
            print(f"✅ {symbol} 止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 止盈止損設置異常 (不影響本地追蹤): {e}")

        # 第四步：安全寫入本地大腦
        positions[symbol] = {'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p, 'sl_price': sl_p,
                             'is_breakeven': False, 'atr': atr}
        cooldown_tracker[symbol] = time.time() + 3600

        log_to_csv({'symbol': symbol, 'action': 'SHORT_ENTRY', 'price': actual_price, 'amount': actual_amount,
                    'trade_value': round(actual_amount * actual_price, 2), 'atr': round(atr, 4),
                    'net_flow': round(net_flow, 2), 'tp_price': tp_p, 'sl_price': sl_p,
                    'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal})

        print(f"📉 [已入貨做空] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 做空核心執行失敗: {e}")


# ==========================================
# 5. 主程序 (頻率分離版)
# ==========================================
def main():
    print(f"🚀 AI 實戰 V6.0 FINAL SHORT (空軍完全體) 啟動...")
    last_scout_time = 0
    while True:
        try:
            manage_short_positions()
            curr_t = time.time()
            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                regime = get_btc_regime()
                if regime == -1:  # 🔴 只有大盤空頭確認才海選
                    print("🔴 紅燈確認：執行空單海選掃描...")
                    target_coins = scouting_weak_coins(5)
                    for s in target_coins:
                        try:
                            flow, last_p, is_weak = apply_lee_ready_short_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0: execute_live_short(s, flow, last_p, is_weak, atr, is_v)
                        except Exception as e:
                            continue
                        time.sleep(0.5)
                last_scout_time = curr_t
                print(f"⏳ 空軍巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")
            time.sleep(POSITION_CHECK_INTERVAL)
        except Exception as e:
            if "10006" in str(e):
                time.sleep(30)
            else:
                time.sleep(10)

        except KeyboardInterrupt:
            print(f"\n👋 指揮官手動終止。餘額: {get_live_usdt_balance():.2f} USDT | 持倉: {list(positions.keys())}")
            sys.exit(0)


if __name__ == "__main__":
    main()