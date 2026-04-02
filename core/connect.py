import yaml
import os
import gc
from pathlib import Path
from loguru import logger

class DataBridge:
    """
    [STAGE 0] Data Bridge Module
    Location: /core/connect.py
    負責從 /config/config.yaml 安全地讀取 API 金鑰與通訊配置。
    """
    VERSION = "1.0.6-Modular-Live"


    def __init__(self):
        # 路徑導航：從 /core/connect.py 向上跳兩級到達根目錄，再進入 /config/
        self.config_path = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'


    def load_bybit_api_config(self, account_name: str):
        """載入指定子帳號的 API 密鑰。"""
        config = self.load_full_config()
        if not config: return None
        acc = config.get('ACCOUNTS', {}).get(account_name)
        if not acc:
            logger.error(f"❌ Account {account_name} not found in ACCOUNTS block.")
            return None
        logger.info(f"✅ API credentials for {account_name} loaded.")
        gc.collect()
        return acc


    def load_tg_config(self):
        """載入 Telegram 配置，具備大小寫不敏感保護。"""
        config = self.load_full_config()
        if not config: return {}
        # 同時檢查 TG_BOT 與 tg_bot 以確保穩健性
        tg = config.get('TG_BOT') or config.get('tg_bot')
        if not tg:
            logger.warning("⚠️ TG_BOT configuration not found in config.yaml.")
            return {}
        return tg