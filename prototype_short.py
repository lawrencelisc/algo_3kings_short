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
API_KEY = "1VjRtJ4cjuJiFk2wFs"
API_SECRET = "s5N38enwd75l0CxvIFLPFWWWmAbj2YxK941j"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    # 'hostname': 'bytick.com',
})
exchange.load_markets()

LOG_DIR = "result"
STATUS_DIR = "status"
LOG_FILE = f"{LOG_DIR}/live_short_log.csv"
STATUS_FILE = f"{STATUS_DIR}/btc_regime_short.csv"

if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

positions, cooldown_tracker = {}, {}

# 👑 策略參數 (做空版)
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01

# 🚀 新增：第一優先防護網 - 單筆最大名義價值上限 (防止 ATR 過低導致買入天文數字)
MAX_NOTIONAL_PER_TRADE = 200.0

# ❌ 舊代碼保留
# NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0
# SCOUTING_INTERVAL = 120
# POSITION_CHECK_INTERVAL = 10
# MIN_NOTIONAL = 5.0
# MIN_IMBALANCE_RATIO = 0.2  # 要求的失衡比例 (可微調)

# 🚀 修正：大幣空軍專用設定 (專打 BTC, ETH, SOL 等流動性霸主)
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.2, 3.0, 1.5, 1.0
MIN_IMBALANCE_RATIO = 0.15  # 大幣流動性好，15% 賣盤失衡已經夠力推跌
SCOUTING_INTERVAL = 125  # from 120 to 125
# ❌ 舊代碼保留
# POSITION_CHECK_INTERVAL = 12        # from 10 to 12
# 🚀 修正：縮短監控間隔，從 12s 改為 4s，提高追蹤止損靈敏度
POSITION_CHECK_INTERVAL = 4
MIN_NOTIONAL = 5.0

# 🚀 新增：強制鎖利參數 (Profit Lock)
PROFIT_LOCK_THRESHOLD = 0.010  # 啟動門檻：當利潤達到 1.5% 時啟動回撤保護
PROFIT_RETRACE_LIMIT = 0.3  # 容忍回撤：如果利潤從最高點回落 40% (例如 5% 跌回 3%)，立即觸發平倉

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

STATUS_COLUMNS = ['timestamp', 'btc_price', 'target_price', 'sma20', 'sma50', 'signal_code', 'decision_text']


# ==========================================
# 1. 核心輔助模組 (新增獨立 CSV 處理 Function)
# ==========================================
def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(LOG_FILE, mode='a', index=False,
                                                    header=not os.path.exists(LOG_FILE))


def log_status_to_csv(data_dict):
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 寫入專屬的 09_btc_status_live.csv
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(STATUS_FILE, mode='a', index=False,
                                                       header=not os.path.exists(STATUS_FILE))


def process_native_exit_log(symbol, pos, position_type='short'):
    """🚨 新增：獨立處理交易所自動平倉的 PnL 結算與 CSV 紀錄"""
    real_exit_price = pos['entry_price']
    real_pnl = 0.0

    try:
        # 🎯 嘗試獲取 Bybit 官方最精準結算單 (含手續費)
        pnl_res = exchange.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'limit': 1
        })

        if pnl_res and pnl_res.get('result') and pnl_res['result'].get('list'):
            last_trade = pnl_res['result']['list'][0]
            real_exit_price = float(last_trade['avgExitPrice'])
            real_pnl = float(last_trade['closedPnl'])
        else:
            raise ValueError("Bybit PnL list is empty")

    except Exception as e:
        logger.debug(f"⚠️ {symbol} 獲取真實 PnL 失敗，使用備用估算: {e}")
        try:
            curr_p = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            # Short PnL: (入場價 - 現價) * 數量
            real_pnl = round((pos['entry_price'] - curr_p) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol,
        'action': 'NATIVE_EXIT',
        'price': real_exit_price,
        'amount': pos['amount'],
        'reason': 'Bybit Native TP/SL',
        'realized_pnl': real_pnl
    })

    return real_pnl


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """核彈級撤單：清理所有掛單與倉位綁定的 TP/SL"""
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'})
    except:
        pass
    try:
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'takeProfit': "0",
            'stopLoss': "0",
            'positionIdx': 0
        })
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

        # ❌ 舊代碼保留
        # return atr, (atr / df['c'].iloc[-1]) > 0.0005
        # 🚀 修正：第二優先防護網 - 提高 ATR 最小值過濾 (死魚幣過濾)，波幅 < 0.15% 直接放棄
        return atr, (atr / df['c'].iloc[-1]) > 0.0015
    except:
        return None, False


def get_3_layer_avg_price(symbol, side='bids'):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([level[0] for level in levels]) / len(levels)
    except:
        return None


def get_btc_regime():
    """🚀 終極導航：HMA 交叉 + ADX 趨勢過濾 + 均量過濾"""
    try:
        # ⚠️ 必須拉長到 150，確保 HMA50 和 ADX 有足夠數據計算
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=150)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df['c'].iloc[-1]
        curr_v = df['v'].iloc[-1]

        # ==========================================
        # 1️⃣ 極速趨勢引擎：計算 HMA 20 與 HMA 50
        # ==========================================
        def calc_hma(s, period):
            half_length = int(period / 2)
            sqrt_length = int(np.sqrt(period))
            # WMA (加權移動平均) 輔助函數
            weights_half = np.arange(1, half_length + 1)
            weights_full = np.arange(1, period + 1)
            weights_sqrt = np.arange(1, sqrt_length + 1)

            wma_half = s.rolling(half_length).apply(lambda x: np.dot(x, weights_half) / weights_half.sum(), raw=True)
            wma_full = s.rolling(period).apply(lambda x: np.dot(x, weights_full) / weights_full.sum(), raw=True)

            s_diff = (2 * wma_half) - wma_full
            hma = s_diff.rolling(sqrt_length).apply(lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True)
            return hma

        df['hma20'] = calc_hma(df['c'], 20)
        df['hma50'] = calc_hma(df['c'], 50)

        # 條件 1：HMA20 跌穿 HMA50 (無滯後跌勢確立)
        hma20_val = df['hma20'].iloc[-1]
        hma50_val = df['hma50'].iloc[-1]
        cond_trend = hma20_val < hma50_val

        # ==========================================
        # 2️⃣ 趨勢強度濾網：計算 ADX (14)
        # ==========================================
        df['up'] = df['h'] - df['h'].shift(1)
        df['down'] = df['l'].shift(1) - df['l']
        df['+dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
        df['-dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))

        atr_14 = df['tr'].ewm(alpha=1 / 14, adjust=False).mean()
        plus_di = 100 * (pd.Series(df['+dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
        minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx_val = dx.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]

        # 條件 2：ADX > 22 (過濾無方向橫盤)
        cond_adx = adx_val > 22

        # ==========================================
        # 3️⃣ 成交量濾網 (抗極端值優化版)：24H 中位數 (Median)
        # ==========================================
        # 改用 24 小時中位數，完美無視單一巨量插針的干擾
        median_v_24 = df['v'].rolling(24).median().iloc[-1]

        # 動能容錯：只需要大於中位數的 80% (0.8)，就視為有足夠健康動能
        target_vol = median_v_24 * 0.8
        cond_vol = curr_v > target_vol

        # ==========================================
        # 4️⃣ 整合訊號與輸出
        # ==========================================
        tick_t = "✅" if cond_trend else "❌"
        tick_a = f"✅ (ADX: {adx_val:.1f})" if cond_adx else f"❌ (ADX: {adx_val:.1f})"
        tick_v = f"✅ (Vol: {curr_v:.0f} > 目標:{target_vol:.0f})" \
            if cond_vol else f"❌ (Vol: {curr_v:.0f} < 目標:{target_vol:.0f})"

        # 必須三個條件同時滿足才開綠燈
        if cond_trend and cond_adx and cond_vol:
            status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
        else:
            status, signal = "🔴 RED     (Sideways / Bullish)", -1

        # 兼容 CSV 紀錄 (借用欄位名)
        report = {
            'btc_price': round(curr_p, 2),
            'target_price': round(hma50_val, 2),
            'sma20': round(hma20_val, 2),
            'adx': round(adx_val, 2),  # 借用位置記錄 ADX
            'signal_code': signal,
            'decision_text': status
        }
        log_status_to_csv(report)

        print("-" * 60)
        print(f"📊 BTC 實時戰報 (HMA+ADX+Vol版) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 極速趨勢: HMA20({hma20_val:.0f}) < HMA50({hma50_val:.0f}) {tick_t}")
        print(f"2️⃣ 趨勢強度: ADX > 22 {tick_a}")
        print(f"3️⃣ 動能確認: 當前量 > 24H中位數(80%) {tick_v}")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return signal
    except Exception as e:
        print(f"⚠️ 導航故障: {e}")
        return 0


# 🚀 新增：啟動時同步交易所真實倉位 (防止重啟導致孤兒倉)
def sync_positions_on_startup():
    print("🔄 正在同步交易所現有倉位...")
    try:
        live_positions_raw = exchange.fetch_positions()
        # 篩選出所有數量大於 0 的真實倉位
        live_symbols = [p for p in live_positions_raw if float(p.get('contracts', 0) or p.get('size', 0)) > 0]

        recovered_count = 0
        for p in live_symbols:
            symbol = p['symbol']
            # 只恢復空單 (short)，如果你同時有跑 long，可能需要額外區分 side
            if p.get('side') == 'sell' or float(p.get('positionValue', 0)) < 0 or (
                    p.get('info') and p['info'].get('side') == 'Sell'):

                entry_price = float(p.get('entryPrice', 0))
                amount = float(p.get('contracts', 0) or p.get('size', 0))

                # 嘗試讀取交易所現有的 SL/TP，如果沒有就重新計算一個安全的初始值
                sl_p = float(p.get('stopLoss', 0))
                tp_p = float(p.get('takeProfit', 0))

                # 若獲取不到歷史 ATR，給予預設假值 (後續迴圈會更新)
                atr, _ = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01

                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price + (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price - (TP_ATR_MULT * atr)))

                # 寫回本地記憶體
                positions[symbol] = {
                    'amount': amount,
                    'entry_price': entry_price,
                    'tp_price': tp_p,
                    'sl_price': sl_p,
                    'is_breakeven': False,
                    'atr': atr,
                    'max_pnl_pct': 0.0  # 重啟時重新計算最高利潤
                }
                recovered_count += 1
                print(f"✅ 成功尋回孤兒空單: {symbol} | 入場價: {entry_price}")

        print(f"🔄 同步完成！共尋回 {recovered_count} 個倉位。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def scouting_weak_coins(n=5):
    try:
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                ask = t.get('ask')
                bid = t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    # 🚀 嚴格大幣門檻：差價必須 < 0.1%
                    if spread < 0.0010:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        # 🚀 大幣專屬：全市場成交量 Top 20% 先有資格入選
        dynamic_min_volume = df['volume'].quantile(0.8)
        df_filtered = df[df['volume'] >= dynamic_min_volume]

        # 尋找跌得最勁 (最弱勢) 的幣種
        return df_filtered.sort_values('change', ascending=True).head(n)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Scouting Error: {e}")
        return []


# 🚀 新增：Short 版專用反向 Lee-Ready 狙擊模式 (含大單加權、加速度與防托盤陷阱)
def apply_lee_ready_short_logic(symbol):
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        # 🚀 修復 Pandas Bug
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)

        # 🚀 大單加權 (大於平均量 2 倍的單，權重 x 2)
        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        # 計算長短窗
        short_window_flow = df['net_flow'].tail(50).sum()

        # 🚀 加速度 (Acceleration) - 比較最近 25 筆與前 25 筆的資金流
        recent_25_flow = df['net_flow'].tail(25).sum()
        prev_25_flow = df['net_flow'].iloc[-50:-25].sum()
        acceleration = recent_25_flow - prev_25_flow

        # 訂單簿失衡
        try:
            ob = exchange.fetch_order_book(symbol, limit=20)
            bids_vol = sum([b[1] for b in ob['bids']])
            asks_vol = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_weak = False
        z_score = 0
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / df['net_flow'].std()

        # ================= 🚀 Short 反向判斷核心 =================

        # 🎯 條件 1 [向下極速狙擊]：短窗負流入 + 負加速度(跌勢加劇) + 賣牆極厚 (Imbalance < -0.15)
        if (short_window_flow < 0) and (acceleration < 0) and (imbalance < -0.15):
            is_weak = True
            print(f"🔥 {symbol} Short Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")

        # 🎯 條件 2 [跌勢確認]：Z-Score 向下擊穿設定門檻
        elif z_score < -NET_FLOW_SIGMA:
            is_weak = True
            print(f"📉 {symbol} Short Z-Score Validated: {z_score:.2f}")

        # 🛡️ 終極防被挾倉 (Short Squeeze) 機制：如果跌得急，但下面有極強買盤托市 (Imbalance > 0.1)
        if is_weak and imbalance > 0.1:
            is_weak = False
            print(f"⚠️ {symbol} 發現莊家托盤陷阱！買盤極厚，取消做空！")

        # 🚀 回傳 3 個變數，完美契合原型機的 main() 迴圈
        return short_window_flow, df['price'].iloc[-1], is_weak

    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


# ==========================================
# 3. 持倉管理 (Short)
# ==========================================
def manage_short_positions():
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉，處理真實 PnL 結算單: {s}")

                # 攞返賺蝕結果
                real_pnl = process_native_exit_log(s, positions[s], position_type='short')  # long版就寫 'long'

                cancel_all_v5(s)

                # 🚀 新增非對稱冷卻邏輯：如果贏錢 (PnL > 0)，即刻剷走冷卻時間！
                if real_pnl > 0:
                    print(f"🏆 {s} 贏錢平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                del positions[s]
                continue

        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (pos['entry_price'] - curr_p) / pos['entry_price']
            sl_updated = False

            # 🚀 新增：追蹤最高利潤點，用於鎖利機制
            if 'max_pnl_pct' not in pos:
                pos['max_pnl_pct'] = pnl_pct
            pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

            # ❌ 舊代碼保留
            # if not pos['is_breakeven'] and pnl_pct > 0.003:
            #     pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 0.9998, True, True

            # 🚀 新增：優化 Short Trail SL 頻率控制 + 喚醒本地 IOC 平倉機制
            if not pos['is_breakeven'] and pnl_pct > 0.003:
                # 跌超過 0.3% 時，將 SL 下移到保本位置 (例如入場價跌 0.15% 處)
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 0.9985, True, True

            if pos['is_breakeven']:
                trail_sl = curr_p + (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl < pos['sl_price']:
                    # ❌ 舊代碼保留：計算：新止損線必須比舊止損線低超過 0.2%，先會 send API，避免 Rate Limit
                    # if (pos['sl_price'] - trail_sl) / pos['sl_price'] > 0.002:
                    # 🚀 修正：降低更新門檻 (從 0.2% 降到 0.05%)，解決 Bybit 止損單更新不及時問題
                    if (pos['sl_price'] - trail_sl) / pos['sl_price'] > 0.0005:
                        sl_updated = True
                    pos['sl_price'] = trail_sl  # 但本地腦海中永遠維持最新線位

            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop(
                        {'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                         'tpslMode': 'Full', 'positionIdx': 0})
                # ❌ 舊代碼保留
                # except:
                #     pass
                # 🚀 修正：捕獲 API 報錯並印出，防止被 Rate Limit 或其他錯誤蒙蔽
                except Exception as e:
                    logger.warning(f"⚠️ {s} 追蹤止損 API 更新失敗 (本地腦海仍保持最新): {e}")

            exit_reason = None

            # 🚀 新增：利潤回撤保護 (防 7U 變 -3U 的殺手鐧)
            if pos['max_pnl_pct'] > PROFIT_LOCK_THRESHOLD:
                # 如果當前利潤回落幅度超過容忍比例 (例如 5% 利潤回落 40%，即跌破 3%)
                if pnl_pct < (pos['max_pnl_pct'] * (1 - PROFIT_RETRACE_LIMIT)):
                    exit_reason = "Profit Retrace Lock (Short IOC Exit)"

            # 只有當沒有觸發回撤保護時，才檢查常規的 TP/SL
            if not exit_reason:
                if curr_p <= pos['tp_price']:
                    exit_reason = "TP (Short IOC Exit)"
                elif curr_p >= pos['sl_price']:
                    # 🚀 修復盲點：移除 and not pos['is_breakeven']，區分出 Trail SL
                    if pos['is_breakeven']:
                        exit_reason = "Trail SL (Short IOC Exit)"
                    else:
                        exit_reason = "SL (Short IOC Exit)"

            if exit_reason:
                print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平空單: {s} | Max PnL: {pos['max_pnl_pct'] * 100:.2f}%")
                try:
                    ioc_price = get_3_layer_avg_price(s, 'asks') or curr_p
                    exchange.create_order(s, 'limit', 'buy', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_buy_order(s, pos['amount'], {'reduceOnly': True})

                # 先計好利潤
                ioc_pnl = round((pos['entry_price'] - curr_p) * pos['amount'], 4)

                log_to_csv({'symbol': s, 'action': 'SHORT_EXIT', 'price': curr_p, 'amount': pos['amount'],
                            'reason': exit_reason,
                            'realized_pnl': ioc_pnl})

                cancel_all_v5(s)

                # 🚀 同步非對稱冷卻邏輯：如果主動平倉係贏錢，一樣解除冷卻！
                if ioc_pnl > 0:
                    print(f"🏆 {s} Bot 主動止盈平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                # 🚨 修復：絕對不刪除冷卻時間
                del positions[s]
    except Exception as e:
        if "10006" in str(e): time.sleep(5)


# ==========================================
# 4. 執行入場 (Short)
# ==========================================
def execute_live_short(symbol, net_flow, current_price, is_weak, atr, is_volatile):
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]  # 只有在這裡（時間到期）才可以刪除冷卻！

    if not (is_weak and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)

    # ❌ 舊代碼保留
    # trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
    # 🚀 修正：加入 MAX_NOTIONAL_PER_TRADE 硬性截斷，防止低 ATR 導致天文數字倉位 (防護網 1)
    trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95,
                    MAX_NOTIONAL_PER_TRADE)

    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']: return

    ioc_p = get_3_layer_avg_price(symbol, 'bids') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        # 開空倉
        order = exchange.create_order(symbol, 'limit', 'sell', amount, ioc_p, {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price = ioc_p
        actual_amount = 0

        try:
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 獲取訂單失敗，啟動備用持倉同步: {e}")
            time.sleep(0.5)
            live_pos = exchange.fetch_positions()
            for p in live_pos:
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} IOC 未成交或數量為 0，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price - (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price + (SL_ATR_MULT * atr)))

        expected_profit_margin = (actual_price - tp_p) / actual_price
        if expected_profit_margin < 0.003:
            print(
                f"🟡 放棄做空 [{symbol}]: 預期利潤空間 ({expected_profit_margin * 100:.2f}%) 太細，連手續費都唔夠俾！")
            cancel_all_v5(symbol)
            return

        try:
            exchange.private_post_v5_position_trading_stop(
                {'category': 'linear', 'symbol': exchange.market_id(symbol), 'stopLoss': str(sl_p),
                 'takeProfit': str(tp_p), 'tpslMode': 'Full', 'positionIdx': 0})
            print(f"✅ {symbol} 止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 止盈止損設置異常 (不影響本地追蹤): {e}")

        # ❌ 舊代碼保留
        # positions[symbol] = {'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p, 'sl_price': sl_p,
        #                      'is_breakeven': False, 'atr': atr}
        # 🚀 修正：加入 'max_pnl_pct' 初始化，用於紀錄利潤最高點
        positions[symbol] = {'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p, 'sl_price': sl_p,
                             'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0}

        cooldown_tracker[symbol] = time.time() + 3600  # 嚴格賦予 1 小時冷卻

        log_to_csv({'symbol': symbol, 'action': 'SHORT_ENTRY', 'price': actual_price, 'amount': actual_amount,
                    'trade_value': round(actual_amount * actual_price, 2), 'atr': round(atr, 4),
                    'net_flow': round(net_flow, 2), 'tp_price': tp_p, 'sl_price': sl_p,
                    'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal})

        print(f"📉 [已入貨做空] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 做空核心執行失敗: {e}")


# ==========================================
# 5. 主程序
# ==========================================

def main():
    print(f"🚀 AI 實戰 V6.0 FINAL SHORT 啟動...")
    print(f"Lee-Ready 資金流邏輯 + 訂單簿失衡度 (Imbalance) + P95濾網 [終極做空版] 啟動...")

    sync_positions_on_startup()
    last_scout_time = 0

    # 🚀 宣告一個空名單，防止啟動時報錯
    target_coins = []

    while True:
        try:
            manage_short_positions()
            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                regime = get_btc_regime()

                if regime == 1:
                    print("🟢 綠燈確認：執行空單海選掃描...")

                    # ❌ 舊代碼保留
                    # target_coins = scouting_weak_coins(5)
                    # 🚀 修正：按你要求，確認將大幣海選數量改為 20 名做空
                    target_coins = scouting_weak_coins(20)

                    # 🛡️ 確保這個 for 迴圈是在 if regime == 1 的縮排裡面！
                    for s in target_coins:
                        try:
                            flow, last_p, is_weak = apply_lee_ready_short_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_short(s, flow, last_p, is_weak, atr, is_v)
                        except Exception as e:
                            continue
                        time.sleep(0.5)

                else:
                    print(f"🚦 目前導航狀態為 {regime}，海選暫停。")
                    # 🚀 終極殺招：黃燈或紅燈時，強制清空孤兒名單！
                    target_coins = []

                last_scout_time = curr_t
                print(f"⏳ 空軍巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 指揮官手動終止。餘額: {get_live_usdt_balance():.2f} USDT | 持倉: {list(positions.keys())}")
            sys.exit(0)

        except Exception as e:
            if "10006" in str(e):
                time.sleep(30)
            else:
                time.sleep(10)


if __name__ == "__main__":
    main()