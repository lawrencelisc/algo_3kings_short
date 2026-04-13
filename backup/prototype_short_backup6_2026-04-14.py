import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
import json
from datetime import datetime

# ==========================================
# ⚙️ [系統/參數] 模組初始化與 API 配置
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Short_V6.6_Thermostat')

# Name: yukikaze
API_KEY = ""
API_SECRET = ""

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

# ──────────────────────────────────────────
# 📁 檔案路徑 (Short 版獨立檔案，不與 Long 衝突)
# ──────────────────────────────────────────
LOG_DIR    = "../result"
STATUS_DIR = "../status"
LOG_FILE       = f"{LOG_DIR}/live_short_log.csv"
STATUS_FILE    = f"{STATUS_DIR}/btc_regime_short.csv"
BLACKLIST_FILE = f"{STATUS_DIR}/dynamic_blacklist_short.json"

if not os.path.exists(LOG_DIR):    os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

# 系統狀態記憶體
positions          = {}   # 在途空倉
cooldown_tracker   = {}   # 冷卻封禁
consecutive_losses = {}   # 連虧計數

# ==========================================
# ⚙️ [系統/參數] 策略與風控全局變數
# ==========================================
WORKING_CAPITAL       = 1000.0
MAX_LEVERAGE          = 10.0
RISK_PER_TRADE        = 0.005   # 0.5% 風險
MIN_NOTIONAL          = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

# ── Lee-Ready 空單版門檻 ──
# Short 版：負向資金流 Z-Score 低於 -NET_FLOW_SIGMA 才觸發
NET_FLOW_SIGMA = 1.2

# ── ATR 倍數 ──
# SL 在入場價「上方」(Short 的止損方向)，TP 在「下方」
# SL_ATR_MULT = 1.2：與 Long 版同理，給予足夠噪聲空間
# TP_ATR_MULT = 5.0：空頭版同樣給足下行空間
TP_ATR_MULT = 5.0
SL_ATR_MULT = 1.2

MAX_CONSECUTIVE_LOSSES = 3
DYNAMIC_BAN_DURATION   = 86400   # 24 小時封禁

SCOUTING_INTERVAL      = 125
POSITION_CHECK_INTERVAL = 4

# ── 恆溫器參數 ──
BRAKE_ADX_HIGH_THRESHOLD = 40
TIMEOUT_SECONDS          = 2700  # 45 分鐘殭屍超時

# ──────────────────────────────────────────
# 📋 Short 版海選：尋找「最弱」幣種
# 靜態黑名單 (穩定幣 / 包裝幣，同 Long 版)
# ──────────────────────────────────────────
BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT',  'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT','USDP/USDT:USDT',  'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT',  'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT',   'RLUSD/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT',  'WAVAX/USDT:USDT',
    'stETH/USDT:USDT','cbETH/USDT:USDT','WHT/USDT:USDT'
]

CSV_COLUMNS = [
    'timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value',
    'atr', 'net_flow', 'tp_price', 'sl_price', 'reason',
    'realized_pnl', 'actual_balance', 'effective_balance'
]
STATUS_COLUMNS = [
    'timestamp', 'btc_price', 'target_price', 'hma20', 'hma50',
    'adx', 'signal_code', 'decision_text'
]


# ==========================================
# 🛠️ [輔助模組] 記錄、帳戶與訂單管理
# ==========================================
def log_to_csv(data_dict):
    """空單交易紀錄寫入 CSV"""
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(
        LOG_FILE, mode='a', index=False, header=not os.path.exists(LOG_FILE)
    )


def log_status_to_csv(data_dict):
    """BTC 大盤導航狀態寫入 CSV"""
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(
        STATUS_FILE, mode='a', index=False, header=not os.path.exists(STATUS_FILE)
    )


def process_native_exit_log(symbol, pos):
    """
    處理交易所自動平倉 (Native Exit) 的 PnL 結算

    Short PnL = (入場價 - 出場價) × 數量
    """
    real_exit_price = pos['entry_price']
    real_pnl        = 0.0

    try:
        pnl_res = exchange.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'limit': 1
        })
        if pnl_res and pnl_res.get('result') and pnl_res['result'].get('list'):
            last_trade      = pnl_res['result']['list'][0]
            real_exit_price = float(last_trade['avgExitPrice'])
            real_pnl        = float(last_trade['closedPnl'])
        else:
            raise ValueError("Bybit PnL list is empty")

    except Exception as e:
        logger.debug(f"⚠️ {symbol} 獲取真實 PnL 失敗，使用備用估算: {e}")
        try:
            curr_p          = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            # Short PnL：入場價 - 現價（價跌盈利）
            real_pnl = round((pos['entry_price'] - curr_p) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol, 'action': 'NATIVE_EXIT', 'price': real_exit_price,
        'amount': pos['amount'], 'reason': 'Bybit Native TP/SL', 'realized_pnl': real_pnl
    })
    return real_pnl


def get_live_usdt_balance():
    """獲取帳戶可用 USDT 餘額"""
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """核彈級撤單：清理該幣種所有掛單與 TP/SL"""
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'})
    except:
        pass
    try:
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear', 'symbol': exchange.market_id(symbol),
            'takeProfit': "0", 'stopLoss': "0", 'positionIdx': 0
        })
    except:
        pass


def get_3_layer_avg_price(symbol, side='asks'):
    """獲取訂單簿前 3 檔平均價格"""
    try:
        ob     = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([lv[0] for lv in levels]) / len(levels)
    except:
        return None


def get_market_metrics(symbol):
    """計算 ATR 並過濾低波動率幣種"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df    = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(
            df['h'] - df['l'],
            np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1)))
        )
        atr = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
        if pd.isna(atr) or atr == 0: return None, False
        return atr, (atr / df['c'].iloc[-1]) > 0.0015
    except:
        return None, False


# ==========================================
# 🛠️ [輔助模組] JSON 記憶與動態黑名單
# ==========================================
def save_dynamic_blacklist():
    data = {'consecutive_losses': consecutive_losses, 'cooldown_tracker': cooldown_tracker}
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except:
        pass


def load_dynamic_blacklist():
    global consecutive_losses, cooldown_tracker
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
            consecutive_losses.update(data.get('consecutive_losses', {}))
            cooldown_tracker.update(data.get('cooldown_tracker', {}))
            curr_t  = time.time()
            expired = [k for k, v in cooldown_tracker.items() if v < curr_t]
            for k in expired:
                del cooldown_tracker[k]
                if k in consecutive_losses: del consecutive_losses[k]
            if expired: save_dynamic_blacklist()
        except:
            pass


def handle_trade_result(symbol, pnl):
    """更新連虧計數與冷卻器"""
    global consecutive_losses, cooldown_tracker
    if pnl > 0:
        consecutive_losses[symbol] = 0
        if symbol in cooldown_tracker: del cooldown_tracker[symbol]
    elif pnl < 0:
        consecutive_losses[symbol] = consecutive_losses.get(symbol, 0) + 1
        if consecutive_losses[symbol] >= MAX_CONSECUTIVE_LOSSES:
            cooldown_tracker[symbol] = time.time() + DYNAMIC_BAN_DURATION
        else:
            cooldown_tracker[symbol] = max(
                cooldown_tracker.get(symbol, 0), time.time() + 480
            )
    save_dynamic_blacklist()


# ==========================================
# 🧠 [核心邏輯] BTC 恆溫器 - Short 版 (1m/5m/15m)
# ==========================================
def get_btc_regime_short():
    """
    空單版恆溫器：邏輯與 Long 版完全鏡像翻轉

    Long  版進場條件：15m HMA20 > HMA50 (上升趨勢)
    Short 版進場條件：15m HMA20 < HMA50 (下降趨勢)

    Long  版 HARD_BRAKE：1m/5m 金叉出現 (多頭回來了，空單危險)
    Short 版 HARD_BRAKE：1m/5m 死叉出現 → 這是「空頭加速」非制動？
    ──────────────────────────────────────────────────────────────
    注意：對空單而言，「危險」是行情反彈 (價格向上)。
    因此：
      HARD_BRAKE (空單) = 1m HMA 出現「金叉」(Short squeeze 警報)
                         + 5m HMA 金叉 或 5m ADX 高位回落確認
      SOFT_BRAKE (空單) = 只有 1m HMA 金叉，5m 仍下行健康

    進場信號 (signal = -1 代表熊市環境)：
      GREEN  = 15m HMA20 < HMA50 AND ADX>22 AND 放量 AND 無 HARD_BRAKE
      RED    = HARD_BRAKE 或 15m 趨勢不夠弱
    ──────────────────────────────────────────────────────────────
    """
    try:
        o15 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        o5  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m',  limit=150)
        o1  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m',  limit=150)

        df15 = pd.DataFrame(o15, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df5  = pd.DataFrame(o5,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df1  = pd.DataFrame(o1,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df15['c'].iloc[-1]

        # ── HMA 計算 (與 Long 版相同函數) ──
        def calc_hma(s, period):
            half   = int(period / 2)
            sq     = int(np.sqrt(period))
            w_half = np.arange(1, half + 1)
            w_full = np.arange(1, period + 1)
            w_sq   = np.arange(1, sq + 1)
            wma_h  = s.rolling(half).apply(lambda x: np.dot(x, w_half) / w_half.sum(), raw=True)
            wma_f  = s.rolling(period).apply(lambda x: np.dot(x, w_full) / w_full.sum(), raw=True)
            diff   = (2 * wma_h) - wma_f
            return diff.rolling(sq).apply(lambda x: np.dot(x, w_sq) / w_sq.sum(), raw=True)

        # ── ADX 計算 (與 Long 版相同函數) ──
        def calc_adx(df):
            df = df.copy()
            df['up']   = df['h'] - df['h'].shift(1)
            df['down'] = df['l'].shift(1) - df['l']
            df['+dm']  = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
            df['-dm']  = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
            df['tr']   = np.maximum(df['h'] - df['l'],
                         np.maximum(abs(df['h'] - df['c'].shift(1)),
                                    abs(df['l'] - df['c'].shift(1))))
            atr14    = df['tr'].ewm(alpha=1/14, adjust=False).mean()
            plus_di  = 100 * (pd.Series(df['+dm']).ewm(alpha=1/14, adjust=False).mean() / atr14)
            minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1/14, adjust=False).mean() / atr14)
            denom    = plus_di + minus_di
            dx       = np.where(denom != 0, 100 * abs(plus_di - minus_di) / denom, 0)
            return pd.Series(dx).ewm(alpha=1/14, adjust=False).mean()

        # ── 指標計算 ──
        h15_20, h15_50 = calc_hma(df15['c'], 20), calc_hma(df15['c'], 50)
        h15_20_val, h15_50_val = h15_20.iloc[-1], h15_50.iloc[-1]
        adx15_val = calc_adx(df15).iloc[-1]

        h5_20, h5_50 = calc_hma(df5['c'], 20), calc_hma(df5['c'], 50)
        adx5_series  = calc_adx(df5)
        adx5_val, adx5_prev = adx5_series.iloc[-1], adx5_series.iloc[-2]

        h1_20, h1_50 = calc_hma(df1['c'], 20), calc_hma(df1['c'], 50)

        # ──────────────────────────────────────────────────────
        # 🚨 Short 版制動邏輯 (與 Long 版方向完全相反)
        #
        # Long  危險訊號 = 死叉 (多頭衰竭)
        # Short 危險訊號 = 金叉 (空頭衰竭 / 可能 Short Squeeze)
        #
        # 1m 金叉 = h1_20 > h1_50 (空頭方向的「反彈警報」)
        # 5m 金叉 = h5_20 > h5_50
        # ──────────────────────────────────────────────────────
        h1_golden_cross = h1_20.iloc[-1] > h1_50.iloc[-1]  # 1m 反彈 (Short 危險)
        h5_golden_cross = h5_20.iloc[-1] > h5_50.iloc[-1]  # 5m 反彈 (Short 更危險)
        adx5_high_drop  = (adx5_val < adx5_prev) and (adx5_prev > BRAKE_ADX_HIGH_THRESHOLD)

        hard_brake   = False
        soft_brake   = False
        brake_reason = ""

        if h1_golden_cross:
            if h5_golden_cross:
                hard_brake   = True
                brake_reason = "1m+5m HMA 雙重金叉 (空頭反彈危機)"
            elif adx5_high_drop:
                hard_brake   = True
                brake_reason = "1m 金叉 + 5m ADX 高位回落 (空頭動能衰竭)"
            else:
                soft_brake   = True
                brake_reason = "1m HMA 金叉 (5m 仍下行健康，輕度警戒)"
        elif h5_golden_cross:
            hard_brake   = True
            brake_reason = "5m HMA 金叉 (空頭環境逆轉警報)"
        elif adx5_high_drop:
            soft_brake   = True
            brake_reason = "5m ADX 高位回落 (無金叉，輕度警戒)"

        brake = hard_brake

        # ── 15m 基礎做空條件 ──
        cond_trend  = h15_20_val < h15_50_val          # Short：HMA20 在 HMA50 下方
        cond_adx    = adx15_val > 22
        completed_v = df15['v'].iloc[-2]
        target_vol  = df15['v'].iloc[-25:-1].median() * 0.8
        cond_vol    = completed_v > target_vol

        # ── 進場許可 ──
        if cond_trend and cond_adx and cond_vol and not hard_brake:
            status, signal = "🔴 GREEN-SHORT (下降趨勢 + ADX + 放量確認)", -1
        elif hard_brake:
            status, signal = f"🟢 RED-SHORT  (HARD BRAKE: {brake_reason})", 0
        elif soft_brake:
            # SOFT_BRAKE：空單仍可入場，但 Trail SL 收緊
            status, signal = f"🟡 SOFT-SHORT (SOFT BRAKE: {brake_reason})", -1
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW     (等待趨勢與動能匯聚)", 0
        else:
            status, signal = "🟢 RED-SHORT  (行情偏多，空單暫停)", 0

        log_status_to_csv({
            'btc_price': round(curr_p, 2), 'target_price': round(h15_50_val, 2),
            'hma20': round(h15_20_val, 2), 'hma50': round(h15_50_val, 2),
            'adx': round(adx15_val, 2), 'signal_code': signal, 'decision_text': status
        })

        print("-" * 60)
        print(f"🌡️ BTC 空單恆溫器 (1m/5m/15m) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 15m 趨勢: HMA20({h15_20_val:.0f}) < HMA50({h15_50_val:.0f}) {'✅' if cond_trend else '❌'} (下降趨勢)")
        print(f"2️⃣ 15m 動能: ADX > 22 {'✅' if cond_adx else '❌'} (值: {adx15_val:.1f})")
        if hard_brake:
            print(f"3️⃣ 制動狀態: 🚨 HARD BRAKE ({brake_reason})")
        elif soft_brake:
            print(f"3️⃣ 制動狀態: ⚠️  SOFT BRAKE ({brake_reason})")
        else:
            print(f"3️⃣ 制動狀態: ✅ 安全 (空頭環境穩定)")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return {'signal': signal, 'brake': hard_brake, 'soft_brake': soft_brake, 'brake_reason': brake_reason}

    except Exception as e:
        logger.error(f"⚠️ 空單恆溫器故障: {e}")
        return {'signal': 0, 'brake': True, 'soft_brake': False, 'brake_reason': 'Error'}


# ==========================================
# 📡 [市場掃描] 弱幣空單海選
# ==========================================
def scouting_weak_coins(scouting_coins=20):
    """
    空單版海選：尋找「最弱勢」幣種

    Long  版：按成交量排 Top 20，再按漲幅排序取最強
    Short 版：按成交量排 Top 20，再按漲幅排序取「最弱」(跌最多)
    邏輯鏡像：.sort_values('change', ascending=True) 取最跌幣
    """
    try:
        tickers = exchange.fetch_tickers()
        data    = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                ask, bid = t.get('ask'), t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    if spread < 0.0010:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        top_majors = df.sort_values('volume', ascending=False).head(scouting_coins)
        # ← 核心鏡像：ascending=True → 取跌幅最大 (最弱) 幣種
        return top_majors.sort_values('change', ascending=True).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Short Scouting Error: {e}")
        return []


# ==========================================
# 🔍 [Lee-Ready 引擎] 空單版資金流分析
# ==========================================
def check_flow_health_short(symbol):
    """
    【防守專用 - 空單版】資金流健康雷達

    Long  版：偵測「買盤傾瀉」(Z-Score < -3 → 多頭危險)
    Short 版：偵測「賣盤傾瀉停止 / 買盤回補」(Z-Score > +3 → 空頭危險)

    Flow Reversal (Short Squeeze) = 資金流 Z-Score 突然大幅正向 (買盤湧入)
    Flow Deceleration (賣盤衰退)  = 賣盤動能加速度回落 且 訂單簿買方重新佔優
    """
    try:
        trades = exchange.fetch_trades(symbol, limit=100)
        if not trades or len(trades) < 50: return None

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_std = df['net_flow'].std()
        if flow_std == 0: return None

        flow_mean      = df['net_flow'].mean()
        recent_25_flow = df['net_flow'].tail(25).sum()
        z_score        = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

        # Short 版：Z-Score > +3.0 → 買盤大量湧入 → 空單危險
        if z_score > 3.0:
            return "Flow Reversal (Short Squeeze Detected)"

        # 賣壓加速度偵測 (Short 版：加速度正向 = 賣壓衰退)
        flow_older_25 = df['net_flow'].iloc[-50:-25].sum()
        acceleration  = recent_25_flow - flow_older_25
        accel_z       = acceleration / (flow_std * np.sqrt(25))

        # accel_z > +2.0 且近期資金流正向 → 賣壓在消退
        if accel_z > 2.0 and recent_25_flow > 0:
            try:
                ob        = exchange.fetch_order_book(symbol, limit=20)
                bids_vol  = sum([b[1] for b in ob['bids']])
                asks_vol  = sum([a[1] for a in ob['asks']])
                imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
                # 買方訂單簿重新佔優 (imbalance > +0.15) → 賣壓死亡
                if imbalance > 0.15:
                    return "Flow Deceleration (Sell Momentum Died)"
            except:
                pass

        return None
    except:
        return None


def apply_lee_ready_short_logic(symbol):
    """
    反向 Lee-Ready 空單狙擊模式

    Long  版：net_flow > 0，acceleration > 0，imbalance > +0.15 → 買盤強勢 → 做多
    Short 版：net_flow < 0，acceleration < 0，imbalance < -0.15 → 賣盤強勢 → 做空

    Z-Score 判斷：
    Long  版：z_score > +NET_FLOW_SIGMA
    Short 版：z_score < -NET_FLOW_SIGMA
    """
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        short_window_flow = df['net_flow'].tail(50).sum()
        acceleration      = df['net_flow'].tail(25).sum() - df['net_flow'].iloc[-50:-25].sum()

        try:
            ob        = exchange.fetch_order_book(symbol, limit=20)
            bids_vol  = sum([b[1] for b in ob['bids']])
            asks_vol  = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_strong = False
        z_score   = 0
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))

        # ── 空單核心條件 (三條件全反轉) ──
        if (short_window_flow < 0) and (acceleration < 0) and (imbalance < -0.15):
            is_strong = True
            print(f"🔥 {symbol} Short Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score < -NET_FLOW_SIGMA:
            is_strong = True
            print(f"📉 {symbol} Short Z-Score Validated: {z_score:.2f}")

        # ── 反陷阱保護 (買盤太強時取消空單) ──
        if is_strong and imbalance > 0.1:
            is_strong = False
            print(f"⚠️ {symbol} 發現軋空陷阱！買盤極厚，取消做空！")

        return short_window_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        print(f"⚠️ Short LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


# ==========================================
# 🛡️ [執行與風控] 空單持倉管理與入場執行
# ==========================================
def sync_positions_on_startup():
    """啟動時同步交易所現有空倉"""
    print("🔄 正在同步交易所現有空倉...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols       = [p for p in live_positions_raw
                              if float(p.get('contracts', 0) or p.get('size', 0)) > 0]

        recovered_count = 0
        for p in live_symbols:
            symbol    = p['symbol']
            side      = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()

            # ← Short 版：只接管 short/sell 方向
            if side in ['short', 'sell'] or info_side in ['sell', 'short']:
                entry_price = float(p.get('entryPrice', 0))
                amount      = float(p.get('contracts', 0) or p.get('size', 0))
                sl_p        = float(p.get('stopLoss', 0))
                tp_p        = float(p.get('takeProfit', 0))
                atr, _      = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01

                # Short SL 在入場價上方，TP 在入場價下方
                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price + (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price - (TP_ATR_MULT * atr)))
                is_be = True if (sl_p < entry_price and sl_p > 0) else False

                positions[symbol] = {
                    'amount': amount, 'entry_price': entry_price,
                    'tp_price': tp_p, 'sl_price': sl_p,
                    'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                    'entry_time': time.time()
                }
                recovered_count += 1
                print(f"✅ 成功尋回孤兒空單: {symbol} | 入場價: {entry_price} | 已保本狀態: {is_be}")

        print(f"🔄 同步完成！共尋回 {recovered_count} 個空倉。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def manage_short_positions(regime=None):
    """
    管理在途空倉

    Short 版所有方向鏡像：
    ┌─────────────────────┬──────────────┬──────────────┐
    │ 邏輯                │ Long 版       │ Short 版      │
    ├─────────────────────┼──────────────┼──────────────┤
    │ PnL 計算            │ 現價-入場價   │ 入場價-現價   │
    │ 保本 SL 方向        │ 入場價上方    │ 入場價下方    │
    │ Trail SL 方向       │ 現價-N×ATR   │ 現價+N×ATR   │
    │ Trail SL 移動條件   │ trail > sl   │ trail < sl   │
    │ TP 觸發             │ 現價 >= tp   │ 現價 <= tp   │
    │ SL 觸發             │ 現價 <= sl   │ 現價 >= sl   │
    │ 平倉方向            │ sell (賣出)   │ buy  (買回)   │
    │ IOC 掛單方向        │ bids 均價     │ asks 均價     │
    └─────────────────────┴──────────────┴──────────────┘
    """
    try:
        live_positions_raw = exchange.fetch_positions(params={'category': 'linear'})
        live_symbols = {
            p['symbol']: p for p in live_positions_raw
            if float(p.get('contracts', 0) or p.get('size', 0)) > 0
        }

        # ── 自動接管孤兒空倉 ──
        for s, p in live_symbols.items():
            if s not in positions:
                side      = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()
                if side in ['short', 'sell'] or info_side in ['sell', 'short']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt     = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _  = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01

                    real_entry_time = float(p.get('createdTime') or (time.time() * 1000)) / 1000.0
                    sl_p = float(p.get('stopLoss') or 0)
                    tp_p = float(p.get('takeProfit') or 0)

                    if sl_p == 0: sl_p = float(exchange.price_to_precision(s, entry_p + (SL_ATR_MULT * atr)))
                    if tp_p == 0: tp_p = float(exchange.price_to_precision(s, entry_p - (TP_ATR_MULT * atr)))
                    is_be = True if (sl_p < entry_p and sl_p > 0) else False

                    positions[s] = {
                        'amount': amt, 'entry_price': entry_p, 'tp_price': tp_p, 'sl_price': sl_p,
                        'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                        'entry_time': real_entry_time
                    }
                    print(f"🚨 [系統自癒] 發現並自動接管孤兒空單: {s} | 入場價: {entry_p} | 數量: {amt}")

        # ── 偵測 Bybit 已自動平倉 ──
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉空單，處理真實 PnL 結算: {s}")
                real_pnl = process_native_exit_log(s, positions[s])
                cancel_all_v5(s)
                handle_trade_result(s, real_pnl)
                del positions[s]
                continue

        # ── 逐倉管理 ──
        for s in list(positions.keys()):
            try:
                curr_p = exchange.fetch_ticker(s)['last']
                pos    = positions[s]

                # Short PnL = 入場價 - 現價 (跌了才盈利)
                pnl_pct             = (pos['entry_price'] - curr_p) / pos['entry_price']
                coin_volatility_pct = pos['atr'] / pos['entry_price']
                sl_updated          = False

                if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
                pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

                # ── 保本觸發 (Short：SL 移到入場價下方) ──
                if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                    # Short 保本：SL 設在入場價下方 0.2% (確保即使平倉也有微利)
                    pos['sl_price']    = pos['entry_price'] * 0.998
                    pos['is_breakeven'] = True
                    sl_updated          = True

                if pos['is_breakeven']:
                    # ── 分層 Trail SL (Short 版：SL 在現價上方，隨價格下跌而下移) ──
                    if regime and regime.get('brake', False):
                        # HARD BRAKE (買盤回湧)：極速收緊 → 現價 + 0.3 ATR
                        trail_sl = curr_p + (0.3 * pos['atr'])
                    elif regime and regime.get('soft_brake', False):
                        # SOFT BRAKE：適度收緊 → 現價 + 0.6 ATR
                        trail_sl = curr_p + (0.6 * pos['atr'])
                    elif pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                        # 賣壓衰退：現價 + 0.5 ATR
                        trail_sl = curr_p + (0.5 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 5.0):
                        trail_sl = curr_p + (0.8 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 3.5):
                        trail_sl = curr_p + (1.2 * pos['atr'])
                    else:
                        trail_sl = curr_p + (1.8 * pos['atr'])

                    # Short Trail SL：只有「新 SL < 舊 SL」才移動 (SL 往下追)
                    if trail_sl < pos['sl_price']:
                        if (pos['sl_price'] - trail_sl) / pos['sl_price'] > 0.0005:
                            sl_updated     = True
                            pos['sl_price'] = trail_sl

                if sl_updated:
                    f_sl = exchange.price_to_precision(s, pos['sl_price'])
                    try:
                        exchange.private_post_v5_position_trading_stop({
                            'category': 'linear', 'symbol': exchange.market_id(s),
                            'stopLoss': str(f_sl), 'tpslMode': 'Full', 'positionIdx': 0
                        })
                    except Exception as e:
                        logger.warning(f"⚠️ {s} 空單追蹤止損 API 更新失敗: {e}")

                exit_reason = None
                time_held   = time.time() - pos.get('entry_time', time.time())

                # ── 殭屍超時 ──
                if not exit_reason and time_held > TIMEOUT_SECONDS and pnl_pct < 0.005:
                    exit_reason = "Momentum Timeout (Stalled Zombie)"

                # ── 資金流健康偵測 ──
                curr_t     = time.time()
                last_check = pos.get('last_flow_check', 0)

                if not exit_reason and (curr_t - last_check > 15):
                    pos['last_flow_check'] = curr_t
                    if time_held > 120:
                        flow_status = check_flow_health_short(s)
                        if flow_status == "Flow Reversal (Short Squeeze Detected)":
                            exit_reason = flow_status
                        elif flow_status == "Flow Deceleration (Sell Momentum Died)":
                            if not pos.get('deceleration_detected', False):
                                pos['deceleration_detected'] = True
                                print(f"⚠️ {s} 偵測到賣壓衰退 (Deceleration)！已啟動極限防禦標記！")

                # ── IOC TP/SL 本地觸發 ──
                if not exit_reason:
                    if curr_p <= pos['tp_price']:
                        exit_reason = "TP (Short IOC Exit)"
                    elif curr_p >= pos['sl_price']:
                        exit_reason = "Trail SL (Short IOC Exit)" if pos['is_breakeven'] else "SL (Short IOC Exit)"

                if exit_reason:
                    print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平空: {s} | "
                          f"持倉: {time_held/60:.1f}分鐘 | Max PnL: {pos['max_pnl_pct']*100:.2f}% | "
                          f"現盈虧: {pnl_pct*100:.2f}%")

                    # Short 平倉方向：買回 (buy)，掛 asks 均價
                    ioc_price = get_3_layer_avg_price(s, 'asks') or curr_p
                    try:
                        exchange.create_order(s, 'limit', 'buy', pos['amount'], ioc_price,
                                              {'timeInForce': 'IOC', 'reduceOnly': True})
                    except:
                        exchange.create_market_buy_order(s, pos['amount'], {'reduceOnly': True})

                    ioc_pnl = round((pos['entry_price'] - ioc_price) * pos['amount'], 4)

                    log_to_csv({
                        'symbol': s, 'action': 'SHORT_EXIT', 'price': curr_p,
                        'amount': pos['amount'], 'reason': exit_reason, 'realized_pnl': ioc_pnl
                    })

                    cancel_all_v5(s)
                    handle_trade_result(s, ioc_pnl)
                    del positions[s]

            except Exception as e:
                if "10006" in str(e): time.sleep(5)

    except Exception as e:
        logger.error(f"❌ manage_short_positions 外層錯誤: {e}")


def execute_live_short(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    """
    執行空單入場

    Long  版：買入 (buy)  → SL 下方，TP 上方
    Short 版：賣出 (sell) → SL 上方，TP 下方
    IOC 掛單：Long 用 asks 均價買入 / Short 用 bids 均價賣出
    """
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]

    if atr is None or atr == 0 or current_price == 0: return
    if not (is_strong and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal    = min(WORKING_CAPITAL, actual_bal)

    trade_val = min(
        (eff_bal * RISK_PER_TRADE) / ((SL_ATR_MULT * atr) / current_price),
        eff_bal * MAX_LEVERAGE * 0.95,
        MAX_NOTIONAL_PER_TRADE
    )
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']: return

    # Short 入場：用 bids 均價賣出 (讓買方接盤)
    ioc_p = get_3_layer_avg_price(symbol, 'bids') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        # Short 訂單方向：sell
        order = exchange.create_order(symbol, 'limit', 'sell', amount, ioc_p,
                                      {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price, actual_amount = ioc_p, 0

        try:
            order_detail  = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price  = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 獲取訂單失敗，啟動備用持倉同步: {e}")
            time.sleep(0.5)
            for p in exchange.fetch_positions():
                p_side = p.get('side', '').lower()
                p_info_side = p.get('info', {}).get('side', '').lower()
                if (p['symbol'] == symbol and
                    float(p.get('contracts', 0) or p.get('size', 0)) > 0 and
                    (p_side in ['short', 'sell'] or p_info_side in ['sell', 'short'])):
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price  = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} Short IOC 未成交，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        # Short TP 在入場價下方，SL 在入場價上方
        tp_p = float(exchange.price_to_precision(symbol, actual_price - (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price + (SL_ATR_MULT * atr)))

        # 預期利潤空間驗證 (TP 需在現價 0.3% 以下)
        expected_profit_margin = (actual_price - tp_p) / actual_price
        if expected_profit_margin < 0.003:
            print(f"🟡 放棄做空 [{symbol}]: 預期跌幅空間 ({expected_profit_margin*100:.2f}%) 太細，立即市價平倉！")
            try:
                exchange.create_market_buy_order(symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平空倉失敗！需人工介入: {e}")
            cancel_all_v5(symbol)
            return

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p), 'takeProfit': str(tp_p),
                'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} 空單止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 空單止盈止損設置異常: {e}")

        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price,
            'tp_price': tp_p, 'sl_price': sl_p,
            'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0,
            'entry_time': time.time()
        }
        cooldown_tracker[symbol] = time.time() + 480
        save_dynamic_blacklist()

        log_to_csv({
            'symbol': symbol, 'action': 'SHORT_ENTRY', 'price': actual_price,
            'amount': actual_amount, 'trade_value': round(actual_amount * actual_price, 2),
            'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
            'tp_price': tp_p, 'sl_price': sl_p,
            'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal
        })
        print(f"📉 [已入貨做空] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 做空核心執行失敗: {e}")


# ==========================================
# 🚀 [主程序] 主迴圈與事件驅動
# ==========================================
def main():
    """
    空單版主事件迴圈

    與 Long 版完全鏡像：
    - 恆溫器改為 get_btc_regime_short()
    - 海選改為 scouting_weak_coins() (最弱幣)
    - Lee-Ready 改為 apply_lee_ready_short_logic()
    - 持倉管理改為 manage_short_positions()
    - Log Spam 防護機制完整保留
    """
    print("🚀 AI 實戰 V6.6 SHORT (空單恆溫器版) 啟動...")
    print("Lee-Ready 負向資金流 + 訂單簿失衡度 + AI 變速 Trail SL + 1m/5m/15m 空單恆溫器 初始化中...")
    print(f"📋 關鍵參數: SL_ATR_MULT={SL_ATR_MULT} | TP_ATR_MULT={TP_ATR_MULT} | RISK={RISK_PER_TRADE*100:.1f}%")

    load_dynamic_blacklist()
    sync_positions_on_startup()

    last_scout_time = 0
    target_coins    = []

    # Log Spam 防護
    _last_brake_state = None

    while True:
        try:
            regime = get_btc_regime_short()
            manage_short_positions(regime)

            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:

                if regime.get('brake', False):
                    _current_state = 'HARD'
                elif regime.get('soft_brake', False):
                    _current_state = 'SOFT'
                else:
                    _current_state = 'GREEN'

                # signal == -1 代表空頭環境允許入場
                if regime['signal'] == -1:
                    if _current_state != _last_brake_state:
                        label = '空頭綠燈' if _current_state == 'GREEN' else '軟剎車 (仍允許做空)'
                        print(f"🔴 {label}確認：執行空單弱幣海選掃描...")

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_short_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_short(s, flow, last_p, is_strong, atr, is_v)
                        except Exception:
                            continue
                        time.sleep(0.5)

                else:
                    # HARD_BRAKE 或 行情偏多：凍結空單入場
                    if _current_state != _last_brake_state:
                        brake_reason = regime.get('brake_reason', '行情偏多或 HARD BRAKE')
                        print(f"🚦 空單恆溫器攔截中 (🚨 {brake_reason})，空單海選暫停。")

                _last_brake_state = _current_state

                last_scout_time = curr_t
                target_coins    = scouting_weak_coins(20)
                print(f"⏳ 空軍巡邏完畢 | 空倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 指揮官手動終止。餘額: {get_live_usdt_balance():.2f} USDT | 空倉: {list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ 主迴圈發生未知錯誤: {e}")
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()