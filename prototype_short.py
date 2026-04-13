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
# ⚙️ [系統/參數] 模組初始化與 API 配置 V6.6 RateFixed
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Short_V6.6_RateFixed')

# Name: yukikaze
API_KEY = "1VjRtJ4cjuJiFk2wFs"
API_SECRET = "s5N38enwd75l0CxvIFLPFWWWmAbj2YxK941j"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,          # ccxt 內建令牌桶，自動補充
    'rateLimit': 120,                 # * [Rate Fix] Bybit linear 預設 120ms/request 已足夠
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

# ──────────────────────────────────────────
# 📁 檔案路徑
# ──────────────────────────────────────────
LOG_DIR    = "result"
STATUS_DIR = "../status"
LOG_FILE       = f"{LOG_DIR}/live_short_log.csv"
STATUS_FILE    = f"{STATUS_DIR}/btc_regime_short.csv"
BLACKLIST_FILE = f"{STATUS_DIR}/dynamic_blacklist_short.json"

if not os.path.exists(LOG_DIR):    os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

# 系統狀態記憶體
positions          = {}
cooldown_tracker   = {}
consecutive_losses = {}

# ==========================================
# ⚙️ [系統/參數] 策略與風控全局變數
# ==========================================
WORKING_CAPITAL        = 1000.0
MAX_LEVERAGE           = 10.0
RISK_PER_TRADE         = 0.005
MIN_NOTIONAL           = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

NET_FLOW_SIGMA = 1.2
TP_ATR_MULT    = 5.0
SL_ATR_MULT    = 1.2

MAX_CONSECUTIVE_LOSSES = 3
DYNAMIC_BAN_DURATION   = 86400

SCOUTING_INTERVAL       = 125
POSITION_CHECK_INTERVAL = 4

BRAKE_ADX_HIGH_THRESHOLD = 40
TIMEOUT_SECONDS          = 2700

# ==========================================
# 🚀 [Rate Fix] Regime 緩存設定
# ==========================================
# 問題根源分析：
#   主迴圈每 4 秒執行一次，每次都呼叫 get_btc_regime_short()。
#   每次 regime call = 3x fetch_ohlcv (15m/5m/1m) = 3 個 API 請求。
#   4 秒 × 3 calls = 每分鐘 45 次，已逼近 Bybit 免認證限制 (50/min)。
#   加上 fetch_positions + N×fetch_ticker，必定 10006。
#
# 修復策略：
#   1. Regime 緩存 60 秒 (1m K 線最小意義週期)，60 秒內重用同一結果。
#   2. 持倉 ticker 改用 fetch_tickers (批次單次請求) 取代逐倉 fetch_ticker。
#   3. fetch_positions 緩存 8 秒 (比 POSITION_CHECK_INTERVAL 稍長)。
#   4. ATR/market_metrics 緩存 60 秒 (5m K 線無需每 4 秒重算)。

REGIME_CACHE_TTL   = 60   # * Regime 緩存存活秒數 (1m K 最小週期)
POSITIONS_CACHE_TTL = 8   # * fetch_positions 緩存秒數
ATR_CACHE_TTL      = 60   # * ATR 緩存秒數

_regime_cache      = {'data': None, 'ts': 0}
_positions_cache   = {'data': None, 'ts': 0}
_atr_cache         = {}   # symbol -> {'atr': float, 'is_volatile': bool, 'ts': float}

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
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(
        LOG_FILE, mode='a', index=False, header=not os.path.exists(LOG_FILE)
    )


def log_status_to_csv(data_dict):
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(
        STATUS_FILE, mode='a', index=False, header=not os.path.exists(STATUS_FILE)
    )


def process_native_exit_log(symbol, pos):
    """處理交易所自動平倉 PnL 結算 (Short: 入場價 - 出場價)"""
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
            raise ValueError("empty")
    except Exception as e:
        logger.debug(f"⚠️ {symbol} PnL 備用估算: {e}")
        try:
            curr_p          = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            real_pnl        = round((pos['entry_price'] - curr_p) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol, 'action': 'NATIVE_EXIT', 'price': real_exit_price,
        'amount': pos['amount'], 'reason': 'Bybit Native TP/SL', 'realized_pnl': real_pnl
    })
    return real_pnl


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """核彈級撤單"""
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
    try:
        ob     = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([lv[0] for lv in levels]) / len(levels)
    except:
        return None


def get_market_metrics(symbol):
    """
    計算 ATR — 加入 60 秒緩存

    * [Rate Fix] 原版每 4 秒對每個持倉 symbol 都重新 fetch_ohlcv(50根)。
      持倉 3 個幣 = 每 4 秒 3 次 ohlcv 請求，完全不必要。
      5m K 線每 300 秒才更新一根，60 秒緩存對策略完全無影響。
    """
    cached = _atr_cache.get(symbol)
    if cached and (time.time() - cached['ts']) < ATR_CACHE_TTL:
        return cached['atr'], cached['is_volatile']  # * 命中緩存，0 API 請求

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df    = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(
            df['h'] - df['l'],
            np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1)))
        )
        atr         = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
        is_volatile = (atr / df['c'].iloc[-1]) > 0.0015

        if pd.isna(atr) or atr == 0:
            return None, False

        _atr_cache[symbol] = {'atr': atr, 'is_volatile': is_volatile, 'ts': time.time()}
        return atr, is_volatile
    except:
        return None, False


def get_live_positions_cached():
    """
    fetch_positions 加入 8 秒緩存

    * [Rate Fix] 原版每 4 秒 call fetch_positions 一次 (private API)。
      Bybit private endpoint 限制更嚴，8 秒緩存把請求頻率減半，
      同時對策略影響極小 (持倉變化最快也要數秒才能反映)。
    """
    if (time.time() - _positions_cache['ts']) < POSITIONS_CACHE_TTL and _positions_cache['data'] is not None:
        return _positions_cache['data']  # * 命中緩存

    try:
        data = exchange.fetch_positions(params={'category': 'linear'})
        _positions_cache['data'] = data
        _positions_cache['ts']   = time.time()
        return data
    except Exception as e:
        logger.warning(f"⚠️ fetch_positions 失敗: {e}")
        return _positions_cache['data'] or []


def fetch_tickers_for_positions(symbols):
    """
    批次取得多個持倉的現價

    * [Rate Fix] 原版用 for s in positions: exchange.fetch_ticker(s)
      每個持倉 = 1 次 API 請求。持倉 5 個 = 每 4 秒 5 次請求。
      改用 fetch_tickers(symbols) = 單次請求取得全部現價，
      API 消耗從 N 次降至 1 次，效果顯著。
    """
    if not symbols:
        return {}
    try:
        result  = exchange.fetch_tickers(symbols)
        return {s: t['last'] for s, t in result.items() if t.get('last')}
    except Exception as e:
        logger.warning(f"⚠️ batch fetch_tickers 失敗，逐一降級: {e}")
        # 降級：逐一抓，防止全軍覆沒
        prices = {}
        for s in symbols:
            try:
                prices[s] = exchange.fetch_ticker(s)['last']
                time.sleep(0.05)
            except:
                pass
        return prices


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
# 🧠 [核心邏輯] BTC 空單恆溫器 + 60 秒緩存
# ==========================================
def get_btc_regime_short():
    """
    空單恆溫器 (1m/5m/15m) — 加入 60 秒緩存

    * [Rate Fix] 這是 Rate Limit 爆炸的主兇：
      原版每次主迴圈都呼叫此函數，每次 = 3x fetch_ohlcv。
      主迴圈 4 秒一圈 → 每分鐘 15 次 × 3 = 45 個 ohlcv 請求。
      加上其他請求，必定超過限制。

      修復：60 秒內重用同一 regime 結果。
      為何 60 秒安全？
        - 15m K 線：900 秒才動一根，60 秒緩存幾乎無影響
        - 5m  K 線：300 秒才動一根，60 秒緩存影響極小
        - 1m  K 線：60 秒才動一根，恰好在緩存邊界，可接受
      交易策略決策週期本身是 125 秒 (SCOUTING_INTERVAL)，
      60 秒緩存絕不會造成進場時間落差。
    """
    # * 命中緩存：直接返回，節省 3 次 API 請求
    if (time.time() - _regime_cache['ts']) < REGIME_CACHE_TTL and _regime_cache['data'] is not None:
        return _regime_cache['data']

    try:
        o15 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        o5  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m',  limit=150)
        o1  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m',  limit=150)

        df15 = pd.DataFrame(o15, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df5  = pd.DataFrame(o5,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df1  = pd.DataFrame(o1,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df15['c'].iloc[-1]

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

        h15_20, h15_50 = calc_hma(df15['c'], 20), calc_hma(df15['c'], 50)
        h15_20_val, h15_50_val = h15_20.iloc[-1], h15_50.iloc[-1]
        adx15_val = calc_adx(df15).iloc[-1]

        h5_20, h5_50 = calc_hma(df5['c'], 20), calc_hma(df5['c'], 50)
        adx5_series  = calc_adx(df5)
        adx5_val, adx5_prev = adx5_series.iloc[-1], adx5_series.iloc[-2]

        h1_20, h1_50 = calc_hma(df1['c'], 20), calc_hma(df1['c'], 50)

        # 空單危險訊號：金叉 (買盤回來)
        h1_golden_cross = h1_20.iloc[-1] > h1_50.iloc[-1]
        h5_golden_cross = h5_20.iloc[-1] > h5_50.iloc[-1]
        adx5_high_drop  = (adx5_val < adx5_prev) and (adx5_prev > BRAKE_ADX_HIGH_THRESHOLD)

        hard_brake, soft_brake, brake_reason = False, False, ""

        if h1_golden_cross:
            if h5_golden_cross:
                hard_brake, brake_reason = True, "1m+5m HMA 雙重金叉 (空頭反彈危機)"
            elif adx5_high_drop:
                hard_brake, brake_reason = True, "1m 金叉 + 5m ADX 高位回落"
            else:
                soft_brake, brake_reason = True, "1m HMA 金叉 (5m 仍下行健康，輕度警戒)"
        elif h5_golden_cross:
            hard_brake, brake_reason = True, "5m HMA 金叉 (空頭環境逆轉警報)"
        elif adx5_high_drop:
            soft_brake, brake_reason = True, "5m ADX 高位回落 (無金叉，輕度警戒)"

        cond_trend  = h15_20_val < h15_50_val
        cond_adx    = adx15_val > 22
        completed_v = df15['v'].iloc[-2]
        cond_vol    = completed_v > df15['v'].iloc[-25:-1].median() * 0.8

        if cond_trend and cond_adx and cond_vol and not hard_brake:
            status, signal = "🟢 GREEN-SHORT (下降趨勢 + ADX + 放量確認)", -1
        elif hard_brake:
            status, signal = f"🔴 RED-SHORT  (HARD BRAKE: {brake_reason})", 0
        elif soft_brake:
            status, signal = f"🟡 SOFT-SHORT (SOFT BRAKE: {brake_reason})", 0
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW     (等待趨勢與動能匯聚)", 0
        else:
            status, signal = "🔴 RED-STOP  (大盤偏多，空單暫停)", 0

        log_status_to_csv({
            'btc_price': round(curr_p, 2), 'target_price': round(h15_50_val, 2),
            'hma20': round(h15_20_val, 2), 'hma50': round(h15_50_val, 2),
            'adx': round(adx15_val, 2), 'signal_code': signal, 'decision_text': status
        })

        print("-" * 60)
        print(f"🌡️ BTC 空單恆溫器 (1m/5m/15m) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 15m 趨勢: HMA20({h15_20_val:.0f}) < HMA50({h15_50_val:.0f}) {'✅' if cond_trend else '❌'}")
        print(f"2️⃣ 15m 動能: ADX > 22 {'✅' if cond_adx else '❌'} (值: {adx15_val:.1f})")
        if hard_brake:
            print(f"3️⃣ 制動: 🚨 HARD BRAKE ({brake_reason})")
        elif soft_brake:
            print(f"3️⃣ 制動: ⚠️  SOFT BRAKE ({brake_reason})")
        else:
            print(f"3️⃣ 制動: ✅ 安全")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        result = {
            'signal': signal, 'brake': hard_brake,
            'soft_brake': soft_brake, 'brake_reason': brake_reason
        }
        # * 寫入緩存
        _regime_cache['data'] = result
        _regime_cache['ts']   = time.time()
        return result

    except Exception as e:
        logger.error(f"⚠️ 空單恆溫器故障: {e}")
        # * 故障時：若有舊緩存則繼續用，避免因單次 API 失敗就 brake=True 砍倉
        if _regime_cache['data'] is not None:
            logger.warning("⚠️ 恆溫器使用上次緩存結果繼續運行")
            return _regime_cache['data']
        return {'signal': 0, 'brake': True, 'soft_brake': False, 'brake_reason': 'API Error'}


# ==========================================
# 📡 [市場掃描] 弱幣空單海選
# ==========================================
def scouting_weak_coins(scouting_coins=20):
    """
    空單版海選：最弱幣種

    * [Rate Fix] fetch_tickers() 是單次批次請求，已是最優。
      此函數本身無需修改，呼叫頻率由 SCOUTING_INTERVAL=125 秒控制，合理。
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
        return top_majors.sort_values('change', ascending=True).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Short Scouting Error: {e}")
        return []


# ==========================================
# 🔍 [Lee-Ready 引擎] 空單版資金流分析
# ==========================================
def check_flow_health_short(symbol):
    """防守雷達：Short Squeeze 偵測"""
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

        if z_score > 3.0:
            return "Flow Reversal (Short Squeeze Detected)"

        flow_older_25 = df['net_flow'].iloc[-50:-25].sum()
        acceleration  = recent_25_flow - flow_older_25
        accel_z       = acceleration / (flow_std * np.sqrt(25))

        if accel_z > 2.0 and recent_25_flow > 0:
            try:
                ob        = exchange.fetch_order_book(symbol, limit=20)
                bids_vol  = sum([b[1] for b in ob['bids']])
                asks_vol  = sum([a[1] for a in ob['asks']])
                imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
                if imbalance > 0.15:
                    return "Flow Deceleration (Sell Momentum Died)"
            except:
                pass
        return None
    except:
        return None


def apply_lee_ready_short_logic(symbol):
    """反向 Lee-Ready 空單狙擊"""
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

        z_score   = 0
        is_strong = False
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))

        if (short_window_flow < 0) and (acceleration < 0) and (imbalance < -0.15):
            is_strong = True
            print(f"🔥 {symbol} Short Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score < -NET_FLOW_SIGMA:
            is_strong = True
            print(f"📉 {symbol} Short Z-Score Validated: {z_score:.2f}")

        if is_strong and imbalance > 0.1:
            is_strong = False
            print(f"⚠️ {symbol} 發現軋空陷阱！買盤極厚，取消做空！")

        return short_window_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        print(f"⚠️ Short LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


# ==========================================
# 🛡️ [執行與風控] 空單持倉管理
# ==========================================
def sync_positions_on_startup():
    print("🔄 正在同步交易所現有空倉...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols       = [p for p in live_positions_raw
                              if float(p.get('contracts', 0) or p.get('size', 0)) > 0]
        recovered_count    = 0
        for p in live_symbols:
            symbol    = p['symbol']
            side      = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()
            if side in ['short', 'sell'] or info_side in ['sell', 'short']:
                entry_price = float(p.get('entryPrice', 0))
                amount      = float(p.get('contracts', 0) or p.get('size', 0))
                sl_p        = float(p.get('stopLoss', 0))
                tp_p        = float(p.get('takeProfit', 0))
                atr, _      = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01
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
                print(f"✅ 尋回孤兒空單: {symbol} | 入場價: {entry_price} | 保本: {is_be}")
        print(f"🔄 同步完成！共尋回 {recovered_count} 個空倉。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def manage_short_positions(regime=None):
    """
    空單持倉管理

    * [Rate Fix] 兩項重大改動：
      1. fetch_positions → get_live_positions_cached() (8 秒緩存)
      2. 逐倉 fetch_ticker → 批次 fetch_tickers_for_positions() (單次請求)
    """
    try:
        live_positions_raw = get_live_positions_cached()  # * 緩存版
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
                    print(f"🚨 [自癒] 接管孤兒空單: {s} | 入場價: {entry_p}")

        # ── Native Exit 偵測 ──
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉空單: {s}")
                real_pnl = process_native_exit_log(s, positions[s])
                cancel_all_v5(s)
                handle_trade_result(s, real_pnl)
                del positions[s]
                continue

        if not positions:
            return

        # * [Rate Fix] 批次取得所有持倉現價 (1 次請求取代 N 次)
        current_prices = fetch_tickers_for_positions(list(positions.keys()))

        for s in list(positions.keys()):
            try:
                curr_p = current_prices.get(s)
                if curr_p is None:
                    logger.warning(f"⚠️ {s} 無法取得現價，跳過本輪")
                    continue

                pos    = positions[s]
                pnl_pct             = (pos['entry_price'] - curr_p) / pos['entry_price']
                coin_volatility_pct = pos['atr'] / pos['entry_price']
                sl_updated          = False

                if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
                pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

                # ── 保本 ──
                if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                    pos['sl_price']     = pos['entry_price'] * 0.998
                    pos['is_breakeven'] = True
                    sl_updated          = True

                if pos['is_breakeven']:
                    if regime and regime.get('brake', False):
                        trail_sl = curr_p + (0.3 * pos['atr'])
                    elif regime and regime.get('soft_brake', False):
                        trail_sl = curr_p + (0.6 * pos['atr'])
                    elif pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                        trail_sl = curr_p + (0.5 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 5.0):
                        trail_sl = curr_p + (0.8 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 3.5):
                        trail_sl = curr_p + (1.2 * pos['atr'])
                    else:
                        trail_sl = curr_p + (1.8 * pos['atr'])

                    if trail_sl < pos['sl_price']:
                        if (pos['sl_price'] - trail_sl) / pos['sl_price'] > 0.0005:
                            sl_updated      = True
                            pos['sl_price'] = trail_sl

                if sl_updated:
                    f_sl = exchange.price_to_precision(s, pos['sl_price'])
                    try:
                        exchange.private_post_v5_position_trading_stop({
                            'category': 'linear', 'symbol': exchange.market_id(s),
                            'stopLoss': str(f_sl), 'tpslMode': 'Full', 'positionIdx': 0
                        })
                    except Exception as e:
                        logger.warning(f"⚠️ {s} Trail SL 更新失敗: {e}")

                exit_reason = None
                time_held   = time.time() - pos.get('entry_time', time.time())

                if not exit_reason and time_held > TIMEOUT_SECONDS and pnl_pct < 0.005:
                    exit_reason = "Momentum Timeout (Stalled Zombie)"

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
                                print(f"⚠️ {s} 偵測到賣壓衰退！啟動極限防禦標記！")

                if not exit_reason:
                    if curr_p <= pos['tp_price']:
                        exit_reason = "TP (Short IOC Exit)"
                    elif curr_p >= pos['sl_price']:
                        exit_reason = "Trail SL (Short IOC Exit)" if pos['is_breakeven'] else "SL (Short IOC Exit)"

                if exit_reason:
                    print(f"⚔️ {exit_reason} | {s} | {time_held/60:.1f}分 | "
                          f"MaxPnL:{pos['max_pnl_pct']*100:.2f}% | 現:{pnl_pct*100:.2f}%")
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

                    # * [Rate Fix] 平倉後強制清除 positions cache，確保下次立即重新拉倉位
                    _positions_cache['ts'] = 0

            except Exception as e:
                if "10006" in str(e):
                    logger.warning("⚠️ Rate limit hit in position loop, sleeping 10s")
                    time.sleep(10)

    except Exception as e:
        logger.error(f"❌ manage_short_positions 外層錯誤: {e}")


def execute_live_short(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    """空單入場執行"""
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

    ioc_p = get_3_layer_avg_price(symbol, 'bids') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        order = exchange.create_order(symbol, 'limit', 'sell', amount, ioc_p,
                                      {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price, actual_amount = ioc_p, 0
        try:
            order_detail  = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price  = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 訂單確認失敗，備用持倉同步: {e}")
            time.sleep(0.5)
            for p in exchange.fetch_positions():
                p_side = p.get('side', '').lower()
                p_info = p.get('info', {}).get('side', '').lower()
                if (p['symbol'] == symbol and
                    float(p.get('contracts', 0) or p.get('size', 0)) > 0 and
                    (p_side in ['short', 'sell'] or p_info in ['sell', 'short'])):
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price  = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} Short IOC 未成交，撤單退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price - (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price + (SL_ATR_MULT * atr)))

        if (actual_price - tp_p) / actual_price < 0.003:
            print(f"🟡 放棄做空 [{symbol}]: 跌幅空間太細，市價平倉！")
            try:
                exchange.create_market_buy_order(symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平空失敗: {e}")
            cancel_all_v5(symbol)
            return

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p), 'takeProfit': str(tp_p),
                'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} 空單 TP/SL 設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} TP/SL 設置異常: {e}")

        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price,
            'tp_price': tp_p, 'sl_price': sl_p,
            'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0,
            'entry_time': time.time()
        }
        cooldown_tracker[symbol] = time.time() + 480
        # * 新開倉後清除 positions cache，確保下次巡邏即時感知
        _positions_cache['ts'] = 0
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
        logger.error(f"❌ {symbol} 做空執行失敗: {e}")


# ==========================================
# 🚀 [主程序] 主迴圈
# ==========================================
def main():
    print("🚀 AI 實戰 V6.6 SHORT (Rate Limit 修復版) 啟動...")
    print(f"📋 SL={SL_ATR_MULT}×ATR | TP={TP_ATR_MULT}×ATR | "
          f"Regime 緩存={REGIME_CACHE_TTL}s | ATR 緩存={ATR_CACHE_TTL}s | "
          f"Positions 緩存={POSITIONS_CACHE_TTL}s")

    load_dynamic_blacklist()
    sync_positions_on_startup()

    last_scout_time   = 0
    target_coins      = []
    _last_brake_state = None

    while True:
        try:
            regime = get_btc_regime_short()       # * 緩存版：60 秒最多 3 次 ohlcv
            manage_short_positions(regime)         # * 緩存版：批次 ticker + 緩存 positions

            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:

                _current_state = ('HARD' if regime.get('brake') else
                                  'SOFT' if regime.get('soft_brake') else 'GREEN')

                if regime['signal'] == -1:
                    if _current_state != _last_brake_state:
                        label = '空頭綠燈' if _current_state == 'GREEN' else '軟剎車 (仍允許做空)'
                        print(f"🟢 {label}確認：執行弱幣海選掃描...")

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_short_logic(s)
                            atr, is_v = get_market_metrics(s)   # * 緩存版
                            if last_p > 0:
                                execute_live_short(s, flow, last_p, is_strong, atr, is_v)
                        except Exception:
                            continue
                        time.sleep(0.3)   # * [Rate Fix] 0.5 → 0.3s，海選期間稍微放慢但不需太保守

                else:
                    if _current_state != _last_brake_state:
                        print(f"🚦 空單恆溫器攔截 ({regime.get('brake_reason', '行情偏多')})，海選暫停。")

                _last_brake_state = _current_state
                last_scout_time   = curr_t
                target_coins      = scouting_weak_coins(20)
                print(f"⏳ 空軍巡邏 | 空倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 手動終止。餘額: {get_live_usdt_balance():.2f} | 空倉: {list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ 主迴圈錯誤: {e}")
            # * [Rate Fix] 10006 sleep 更長，避免短時間重試加劇問題
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()