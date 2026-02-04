import os
import yaml
import random
import math
import time
import json
import asyncio
import copy
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from astrbot.api import star, logger
from astrbot.api.star import Star, Context, StarTools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.session_lock import session_lock_manager
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from concurrent.futures import ThreadPoolExecutor

# ==================== 常量定义 ====================
PLUGIN_DIR = os.path.dirname(__file__)
PLUGIN_NAME = "astrbot_plugin_pet_market"

# 数据目录将在 __init__ 中使用 get_astrbot_data_path 初始化（符合 astrbot 规范）
DATA_DIR = None  # 延迟初始化，指向 data/plugin_data/{plugin_name}/
DATA_FILE = None  # 延迟初始化，指向 data/plugin_data/{plugin_name}/pet_data.yml
BACKUP_DIR = None  # 延迟初始化，数据备份目录

# 文案文件路径（最好也迁移到数据目录）
COPYWRITING_FILE = os.path.join(PLUGIN_DIR, "resources", "data", "pet_copywriting.json")
TRAIN_COPYWRITING_FILE = os.path.join(PLUGIN_DIR, "resources", "data", "train_copywriting.json")
CARD_TEMPLATE = os.path.join(PLUGIN_DIR, "card_template.html")
MENU_TEMPLATE = os.path.join(PLUGIN_DIR, "menu_template.html")

# 线程池用于异步文件操作
_executor = ThreadPoolExecutor(max_workers=1)

# 默认初始金币
INITIAL_COINS = 150

# 宠物进化阶段
EVOLUTION_STAGES = {
    "普通": {"min": 100, "max": 499, "work_bonus": 0, "train_bonus": 0, "color": "#999999"},
    "稀有": {"min": 500, "max": 1999, "work_bonus": 0.2, "train_bonus": 0, "color": "#4CAF50"},
    "史诗": {"min": 2000, "max": 4999, "work_bonus": 0.4, "train_bonus": 0.1, "color": "#9C27B0"},
    "传说": {"min": 5000, "max": 999999, "work_bonus": 0.6, "train_bonus": 0.15, "color": "#FF9800"}
}

EVOLUTION_COSTS = {
    "稀有": 1000,
    "史诗": 3000
}

# ==================== 商店物品定义 ====================
SHOP_ITEMS = {
    "101": {"name": "精力药水", "price": 500, "desc": "【每日必备】立即重置打工和训练冷却，肝帝首选", "icon": "🧪"},
    "102": {"name": "护身符", "price": 2000, "desc": "【保财神器】自动抵挡一次抢劫，生效后消耗", "icon": "🧿"},
    "104": {"name": "初级刮刮乐", "price": 200, "desc": "【小赌怡情】最高赢 2000 金币 (10倍)，回本率 55%", "icon": "🎫",
            "type": "scratch_card", 
            "awards": [
                {"name": "谢谢惠顾", "prob": 0.45, "amount": 0},
                {"name": "安慰奖", "prob": 0.20, "amount": 20},
                {"name": "回本奖", "prob": 0.15, "amount": 100},
                {"name": "小赚一比", "prob": 0.10, "amount": 200},
                {"name": "运气不错", "prob": 0.08, "amount": 500},
                {"name": "手气爆棚", "prob": 0.018, "amount": 1000},
                {"name": "天选之子", "prob": 0.002, "amount": 2000},
            ]},
    "105": {"name": "宠物零食", "price": 300, "desc": "【养成必备】喂食增加 20-50 身价，提升PK胜率", "icon": "🦴"},
    "106": {"name": "高级刮刮乐", "price": 1000, "desc": "【搏一搏】最高赢 10000 金币 (10倍)，有机会暴富", "icon": "🎫",
             "type": "scratch_card",
             "awards": [
                 {"name": "谢谢惠顾", "prob": 0.50, "amount": 0},
                 {"name": "安慰奖", "prob": 0.20, "amount": 100},
                 {"name": "回本奖", "prob": 0.15, "amount": 500},
                 {"name": "小赚一比", "prob": 0.10, "amount": 1200},
                 {"name": "财神附体", "prob": 0.04, "amount": 3000},
                 {"name": "超级大奖", "prob": 0.01, "amount": 10000},
             ]},
    "107": {"name": "基因药剂", "price": 2000, "desc": "【高风险】30%概率身价翻倍，70%概率身价减半", "icon": "💉"},
    "108": {"name": "潘多拉魔盒", "price": 2000, "desc": "【极致心跳】8%赢10倍大奖，但也有大概率坐牢或破产", "icon": "📦"},
    "109": {"name": "走私货物", "price": 5000, "desc": "【创业路】50%大赚数千金币，50%被没收且罚款", "icon": "💼"},
}


# ==================== 市场管理器 ====================
class MarketManager:
    def __init__(self, data_file: Path):
        self.data_file = data_file
        self.market_data = {
            "last_update": 0,
            "instruments": {}
        }
        self.default_instruments = {
            # 基金（稳健型 - 波动极小）
            "F101": {"name": "国债逆回购", "type": "fund", "base_price": 1.0, "volatility": 0.001, "desc": "几乎无风险，收益如止水", "drift": 0.00005},
            "F102": {"name": "稳健债基A", "type": "fund", "base_price": 1.1, "volatility": 0.002, "desc": "主投债券，稳稳的幸福", "drift": 0.00008},
            "F103": {"name": "沪深300ETF", "type": "fund", "base_price": 3.5, "volatility": 0.010, "desc": "跟随大盘，长期投资首选", "drift": 0.00012},
            "F104": {"name": "纳指科技基", "type": "fund", "base_price": 2.8, "volatility": 0.015, "desc": "聚焦海外科技，波动稍大", "drift": 0.00015},

            # 股票（平衡型 - 波动适中）
            # 科技/半导体板块
            "S201": {"name": "橘猫科技", "type": "stock", "base_price": 25.0, "volatility": 0.12, "desc": "互联网巨头，业绩优良", "drift": 0.0001},
            "S202": {"name": "汪汪半导体", "type": "stock", "base_price": 45.0, "volatility": 0.18, "desc": "国产芯片之光，受周期影响", "drift": 0.0},
            # 消费/医药板块
            "S203": {"name": "锦鲤酒业", "type": "stock", "base_price": 120.0, "volatility": 0.08, "desc": "高端酱香型，永远的神", "drift": 0.0001},
            "S204": {"name": "治愈生物", "type": "stock", "base_price": 30.0, "volatility": 0.10, "desc": "创新药企，研发风险较高", "drift": 0.0},
            # 工业/能源板块
            "S205": {"name": "阿柴重工", "type": "stock", "base_price": 12.0, "volatility": 0.06, "desc": "基建狂魔，低估值高分红", "drift": 0.0},
            "S206": {"name": "二哈新能源", "type": "stock", "base_price": 18.0, "volatility": 0.15, "desc": "光伏锂电，大起大落", "drift": 0.0},
            # 传媒/AI板块
            "S207": {"name": "幻影传媒", "type": "stock", "base_price": 9.0, "volatility": 0.20, "desc": "短剧游戏概念，妖股体质", "drift": 0.0},

            # 虚拟币（激进型 - 波动剧烈）
            "C301": {"name": "比特币 BTC", "type": "crypto", "base_price": 60000.0, "volatility": 0.25, "desc": "数字黄金，相对抗跌", "drift": 0.0003},
            "C302": {"name": "以太坊 ETH", "type": "crypto", "base_price": 3000.0, "volatility": 0.30, "desc": "智能合约之王，应用广泛", "drift": 0.0003},
            "C303": {"name": "狗狗币 DOGE", "type": "crypto", "base_price": 0.2, "volatility": 0.45, "desc": "Meme币鼻祖，马斯克带货", "drift": 0.0},
            "C304": {"name": "笑脸币 SLILE", "type": "crypto", "base_price": 0.01, "volatility": 0.80, "desc": "土狗项目，归零或百倍", "drift": 0.0},
        }
        self._load_market()

    def _load_market(self):
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    saved_data = json.load(f)
                    self.market_data.update(saved_data)
                    # Merge new instruments if any
                    for code, info in self.default_instruments.items():
                        if code not in self.market_data["instruments"]:
                            self._init_instrument(code, info)
            except Exception as e:
                logger.error(f"加载市场数据失败: {e}")
                self._init_market()
        else:
            self._init_market()

    def _init_market(self):
        self.market_data["last_update"] = int(time.time())
        self.market_data["instruments"] = {}
        for code, info in self.default_instruments.items():
            self._init_instrument(code, info)
        self.save_market()

    def _init_instrument(self, code, info):
        self.market_data["instruments"][code] = {
            "name": info["name"],
            "type": info["type"],
            "current_price": info["base_price"],
            "price_history": [info["base_price"]] * 10,
            "change_24h": 0.0,
            "desc": info["desc"]
        }

    def save_market(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.market_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存市场数据失败: {e}")

    def update_market(self):
        """更新市场价格，模拟真实波动"""
        instruments = self.market_data["instruments"]
        
        for code, data in instruments.items():
            default = self.default_instruments.get(code, {})
            volatility = default.get("volatility", 0.05)
            drift = default.get("drift", 0.0)
            
            # 使用几何布朗运动模型简化版 Price(t) = Price(t-1) * e^(drift + sigma * epsilon)
            # 或者更简单的百分比浮动
            
            # 随机波动因子 (-1 到 1 的正态分布 * 波动率)
            shock = random.gauss(0, 1) * volatility
            
            # 趋势项 (基金有微弱上涨趋势)
            trend = drift
            
            # 价格变动
            change_percent = trend + shock
            
            # 限制单次最大涨跌幅，防止通过系统漏洞刷钱，也符合熔断机制
            max_change = volatility * 2
            change_percent = max(min(change_percent, max_change), -max_change)
            
            old_price = data["current_price"]
            new_price = old_price * (1 + change_percent)
            
            # 防止价格归零，设定最低价
            new_price = max(0.01, new_price)
            
            data["current_price"] = round(new_price, 4)
            data["price_history"].append(data["current_price"])
            if len(data["price_history"]) > 30: # 保留最近30次记录
                data["price_history"].pop(0)
                
            # 计算24小时(近似最近10次周期)涨跌幅
            start_price = data["price_history"][0] if data["price_history"] else new_price
            data["change_24h"] = (new_price - start_price) / start_price

        self.market_data["last_update"] = int(time.time())
        self.save_market()

    def get_market_summary(self) -> str:
        lines = ["📊 【金融市场大盘】"]
        
        types = {"fund": "🟢 基金", "stock": "🔴 股票", "crypto": "⚡ 虚拟币"}
        
        # 分组展示
        grouped = {"fund": [], "stock": [], "crypto": []}
        for code, data in self.market_data["instruments"].items():
            itype = self.default_instruments.get(code, {}).get("type", "stock")
            grouped[itype].append((code, data))
            
        for itype, label in types.items():
            if not grouped.get(itype): continue
            lines.append(f"\n{label}:")
            for code, data in grouped[itype]:
                price = data['current_price']
                change = data['change_24h']
                icon = "📈" if change >= 0 else "📉"
                lines.append(f"  [{code}] {data['name']}")
                lines.append(f"    现价: {price:.4f} | 幅度: {change:+.2%} {icon}")
        
        lines.append("\n💡 指令：/买入 [代码] [金额] | /卖出 [代码] [全部/份额]")
        return "\n".join(lines)
    
    def get_instrument(self, code_or_name: str):
        instruments = self.market_data["instruments"]
        code_or_name = code_or_name.strip()
        
        # Try direct code match (case insensitive)
        for code, data in instruments.items():
            if code.lower() == code_or_name.lower():
                return code, data
        
        # Try name partial match
        for code, data in instruments.items():
            if code_or_name in data["name"]:
                return code, data
                
        return None, None


# ==================== 主类 ====================
class Main(Star):
    def __init__(self, context: Context, config=None, **kwargs):
        # AstrBot v4+ 会在实例化插件时注入插件配置对象（AstrBotConfig，来源 _conf_schema.json + data/config/*_config.json）。
        # 需要保存该对象引用，才能让 WebUI 保存后的配置热更新生效。
        super().__init__(context, config=config)
        self._plugin_config = config
        self.context = context
        self.pet_data: Dict = {}
        self.copywriting: Dict = {}
        self.train_copywriting: Dict = {}
        self._dirty = False  # 脏数据标记
        self._dirty_version = 0  # 数据变更版本号（用于避免保存竞态）
        self._save_task: Optional[asyncio.Task] = None

        # 【规范化】使用 get_astrbot_data_path 获取标准数据目录
        # 符合 astrbot 规范：data/plugin_data/{plugin_name}/
        global DATA_DIR, DATA_FILE, BACKUP_DIR
        plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        DATA_DIR = plugin_data_path
        DATA_FILE = DATA_DIR / "pet_data.yml"
        BACKUP_DIR = DATA_DIR / "backups"
        MARKET_FILE = DATA_DIR / "market_data.json" # 市场数据文件

        # 【新增】初始化管理员列表
        self.admins = self._init_admins()
        self.debt_queue = [] # 追债队列

        self.market_manager = MarketManager(MARKET_FILE) # 初始化市场管理器

        self._init_env()
        self._load_data()
        self._load_copywriting()

    @property
    def config(self):
        """插件配置（优先使用 WebUI 注入的插件配置对象）。"""
        if getattr(self, "_plugin_config", None) is not None:
            return self._plugin_config
        # 兼容：若未注入插件配置，则回退到 AstrBot 默认配置对象
        return getattr(self.context, "_config", {})

    # ==================== 生命周期管理 ====================
    async def initialize(self):
        """插件初始化"""
        logger.info("[宠物市场] 插件初始化")
        # 启动自动保存任务
        self._save_task = asyncio.create_task(self._auto_save_loop())
        # 启动市场更新任务
        self._market_task = asyncio.create_task(self._market_update_loop())

    async def terminate(self):
        """插件终止"""
        logger.info("[宠物市场] 插件正在关闭")
        # 取消自动保存任务
        if self._save_task:
            self._save_task.cancel()
        if hasattr(self, '_market_task') and self._market_task:
            self._market_task.cancel()
        
        try:
            if self._save_task: await self._save_task
            if hasattr(self, '_market_task') and self._market_task: await self._market_task
        except asyncio.CancelledError:
            pass
        # 最终保存数据
        if self._dirty:
            self._save_data()
        self.market_manager.save_market() # 保存市场数据

    async def _market_update_loop(self):
        """市场自动更新循环"""
        while True:
            try:
                # 每30分钟更新一次市场
                await asyncio.sleep(1800) 
                self.market_manager.update_market()
                logger.info("[宠物市场] 市场行情已刷新")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[宠物市场] 市场更新失败: {e}")
                await asyncio.sleep(60)
        logger.info("[宠物市场] 插件已关闭")

    async def _process_debt_queue(self):
        """处理追债队列"""
        if not self.debt_queue:
            return

        # 取出所有当前任务
        tasks = self.debt_queue[:]
        self.debt_queue = []

        for task in tasks:
            group_id = task["group_id"]
            debtor_id = task["debtor_id"]
            target_id = task["target_id"]
            base_amount = task["amount"] # 原始转账金额限制

            # 排序锁，防止死锁
            lock_ids = sorted([debtor_id, target_id])
            try:
                async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
                    async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                        debtor = self._get_user_data(group_id, debtor_id)
                        target = self._get_user_data(group_id, target_id)
                        
                        debt = debtor.get("loan_amount", 0)
                        if debt <= 0:
                            continue # 已经还清了

                        # 计算最多需要追回多少（不能超过债务，也不能超过当时的转账额）
                        max_clawback = min(amount for amount in [base_amount, debt])
                        
                        # 1. 扣现金
                        target_coins = target.get("coins", 0)
                        deduct_coins = min(target_coins, max_clawback)
                        
                        target["coins"] -= deduct_coins
                        debtor["loan_amount"] -= deduct_coins
                        
                        remaining_need = max_clawback - deduct_coins
                        
                        # 2. 扣存款
                        deduct_bank = 0
                        if remaining_need > 0:
                             target_bank = target.get("bank", 0)
                             deduct_bank = min(target_bank, remaining_need)
                             if deduct_bank > 0:
                                 target["bank"] -= deduct_bank
                                 debtor["loan_amount"] -= deduct_bank
                        
                        total_deducted = deduct_coins + deduct_bank
                        
                        if total_deducted > 0:
                             # 记录一些信息让用户知道
                             target_name = task.get("target_name", target_id)
                             logger.info(f"[{group_id}] 追债成功：从 {target_name}({target_id}) 追回 {total_deducted}")
                             
                             debtor["last_clawback_msg"] = f"成功从 {target_name} 处追回 {total_deducted} 金币抵债"
                             target["last_clawback_msg"] = f"因 {debtor_id} 贷款逾期，银行强制收回了其向您转移的资金 {total_deducted} 金币"

                             self._save_user_data(group_id, debtor_id, debtor)
                             self._save_user_data(group_id, target_id, target)
                             
            except Exception as e:
                logger.error(f"[追债] 处理任务失败 {task}: {e}")

    async def _auto_save_loop(self):
        """自动保存循环（每60秒，异步执行避免阻塞）"""
        try:
            while True:
                await asyncio.sleep(60)
                await self._process_debt_queue() # 处理追债
                if self._dirty:
                    version_before = self._dirty_version
                    await self._save_data_async()
                    # 仅当保存期间没有新改动，才清除脏标记
                    if self._dirty_version == version_before:
                        self._dirty = False
                        logger.debug("[宠物市场] 自动保存完成")
                    else:
                        logger.debug("[宠物市场] 保存期间检测到新改动，保持脏标记")
        except asyncio.CancelledError:
            logger.debug("[宠物市场] 自动保存任务已取消")
            raise

    # ==================== 数据管理 ====================
    def _init_env(self):
        """初始化环境（确保目录存在，不会被更新清除）"""
        # 创建插件数据目录
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # 创建备份目录
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        
        # 如果数据文件不存在则创建空数据文件
        if not DATA_FILE.exists():
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump({}, f)
            logger.info(f"[宠物市场] 数据文件已初始化：{DATA_FILE}")
        else:
            logger.debug(f"[宠物市场] 数据文件已存在：{DATA_FILE}")

    def _load_data(self):
        """加载数据（带错误恢复机制）"""
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                self.pet_data = data if isinstance(data, dict) else {}
            logger.info(f"[宠物市场] 数据加载成功，共 {len(self.pet_data)} 个群组，路径：{DATA_FILE}")
        except Exception as e:
            logger.error(f"[宠物市场] 数据加载失败: {e}，尝试恢复备份...")
            restored = self._try_restore_backup()
            if not restored:
                self.pet_data = {}

    def _save_data(self):
        """保存数据到文件（同步版本，含备份机制）"""
        try:
            # 1. 如果旧文件存在，先备份
            if DATA_FILE.exists():
                backup_file = BACKUP_DIR / f"pet_data_{int(time.time())}.yml"
                import shutil
                shutil.copy2(DATA_FILE, backup_file)
                logger.debug(f"[宠物市场] 数据备份：{backup_file}")
            
            # 2. 写入新数据
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump(self.pet_data, f, allow_unicode=True, default_flow_style=False)
            logger.debug(f"[宠物市场] 数据保存成功：{DATA_FILE}")
        except Exception as e:
            logger.error(f"[宠物市场] 数据保存失败: {e}")

    def _try_restore_backup(self):
        """尝试从最新备份恢复数据"""
        try:
            if not BACKUP_DIR.exists():
                logger.warning("[宠物市场] 备份目录不存在，无法恢复")
                return False
            
            # 找最新的备份文件
            backup_files = sorted(BACKUP_DIR.glob("pet_data_*.yml"), key=lambda x: x.stat().st_mtime, reverse=True)
            if not backup_files:
                logger.warning("[宠物市场] 未找到备份文件")
                return False
            
            latest_backup = backup_files[0]
            logger.info(f"[宠物市场] 正在从备份恢复：{latest_backup}")
            
            with open(latest_backup, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                self.pet_data = data if isinstance(data, dict) else {}
            
            logger.warning(f"[宠物市场] 数据已从备份恢复，共 {len(self.pet_data)} 个群组")
            return True
        except Exception as e:
            logger.error(f"[宠物市场] 备份恢复失败: {e}")
            return False

    async def _save_data_async(self):
        """异步保存数据（使用线程池避免阻塞）"""
        loop = asyncio.get_event_loop()
        # 创建深拷贝避免并发写入导致的数据不一致
        data_copy = copy.deepcopy(self.pet_data)
        await loop.run_in_executor(_executor, self._write_data_file, data_copy)

    def _write_data_file(self, data: Dict):
        """写入数据文件（在线程池中执行）"""
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            logger.debug(f"[宠物市场] 数据异步保存成功：{DATA_FILE}")
        except Exception as e:
            logger.error(f"[宠物市场] 数据保存失败: {e}")

    def _load_copywriting(self):
        """加载文案"""
        try:
            if os.path.exists(COPYWRITING_FILE):
                with open(COPYWRITING_FILE, "r", encoding="utf-8") as f:
                    self.copywriting = json.load(f)
            else:
                self.copywriting = {"success": ["打工成功！"], "failure": ["打工失败..."]}
        except Exception as e:
            logger.error(f"[宠物市场] 文案加载失败: {e}")
            self.copywriting = {"success": ["打工成功！"], "failure": ["打工失败..."]}

        try:
            if os.path.exists(TRAIN_COPYWRITING_FILE):
                with open(TRAIN_COPYWRITING_FILE, "r", encoding="utf-8") as f:
                    self.train_copywriting = json.load(f)
            else:
                self.train_copywriting = {
                    "success": ["{name} 训练成功，身价提升 {increase}，当前 {value} 金币。"],
                    "failure": ["{name} 训练失败，身价下降 {decrease}，当前 {value} 金币。"]
                }
        except Exception as e:
            logger.error(f"[宠物市场] 训练文案加载失败: {e}")
            self.train_copywriting = {
                "success": ["{name} 训练成功，身价提升 {increase}，当前 {value} 金币。"],
                "failure": ["{name} 训练失败，身价下降 {decrease}，当前 {value} 金币。"]
            }

    # ==================== 用户数据操作 ====================
    def _get_user_data(self, group_id: str, user_id: str) -> Dict:
        """获取用户数据，自动初始化"""
        group_data = self.pet_data.setdefault(group_id, {})

        if user_id in group_data:
            user = group_data[user_id]
            if "loan_amount" not in user:
                user["loan_amount"] = 0
            if "loan_principal" not in user:
                user["loan_principal"] = user.get("loan_amount", 0)
            if "loan_interest_frozen" not in user:
                user["loan_interest_frozen"] = False
            if "last_loan_interest_time" not in user:
                user["last_loan_interest_time"] = int(time.time())
            # 【新增】抢劫失败相关数据
            if "rob_fail_streak" not in user:
                user["rob_fail_streak"] = 0
            if "rob_pending_penalty" not in user:
                user["rob_pending_penalty"] = None

        if user_id not in group_data:
            group_data[user_id] = {
                "coins": INITIAL_COINS,
                "value": 100,
                "pets": [],
                "master": "",
                "nickname": "",
                "cooldowns": {},
                "bank": 0,
                "bank_level": 1,
                "last_interest_time": int(time.time()),
                "loan_amount": 0,  # 总欠款（本金+利息）
                "loan_principal": 0,  # 本金
                "loan_interest_frozen": False,  # 坏账利息冻结标记
                "last_loan_interest_time": int(time.time()),
                "jailed_until": 0,
                "last_active": int(time.time()),
                "initialized": True,
                "transfer_history": [],
                "evolution_stage": "普通",
                # 【新增】抢劫相关
                "rob_fail_streak": 0,  # 连败次数
                "rob_pending_penalty": None,  # 待处理的罚款状态
                # 【新增】投资相关
                "investments": [],  # 投资列表 [{id, type, amount, start_time, status, current_value, trend_history}]
                "next_investment_id": 1  # 投资ID生成器
            }
            self._mark_dirty()
            logger.info(f"[宠物市场] 新用户 {user_id} 初始化，发放 {INITIAL_COINS} 金币")
        return group_data[user_id]

    def _mark_dirty(self):
        """标记数据已变更（带版本号）"""
        self._dirty = True
        self._dirty_version += 1

    def _save_user_data(self, group_id: str, user_id: str, data: Dict):
        """保存用户数据（仅标记脏数据）"""
        data["last_active"] = int(time.time())
        self.pet_data.setdefault(group_id, {})[user_id] = data
        self._mark_dirty()

    def _get_pets_in_group(self, group_id: str) -> Dict:
        """获取群内所有宠物数据"""
        return self.pet_data.get(group_id, {})

    def _remove_user_data(self, group_id: str, user_id: str):
        """删除用户数据"""
        self.pet_data.get(group_id, {}).pop(user_id, None)
        self._mark_dirty()

    # ==================== 工具方法 ====================
    def _check_jailed(self, group_id: str, user_id: str) -> Tuple[bool, int]:
        """检查是否在监狱中
        Returns:
            (是否在狱, 剩余秒数)
        """
        user = self._get_user_data(group_id, user_id)
        jailed_until = user.get("jailed_until", 0)
        now = int(time.time())
        if jailed_until > now:
            return True, jailed_until - now
        return False, 0

    def _check_cooldown(self, user_data: Dict, cooldown_type: str, cooldown_seconds: int) -> Tuple[bool, int]:
        """检查冷却时间
        Returns:
            (是否在冷却中, 剩余秒数)
        """
        cooldowns = user_data.get("cooldowns", {})
        last_time = cooldowns.get(cooldown_type, 0)
        now = int(time.time())
        if now - last_time < cooldown_seconds:
            remain = cooldown_seconds - (now - last_time)
            return True, remain
        return False, 0

    def _set_cooldown(self, user_data: Dict, cooldown_type: str):
        """设置冷却时间"""
        cooldowns = user_data.setdefault("cooldowns", {})
        cooldowns[cooldown_type] = int(time.time())

    def _extract_target(self, event: AstrMessageEvent) -> Optional[str]:
        """提取目标用户ID（优先使用@，避免歧义）"""
        # 优先从 At 组件提取（推荐方式）
        at_targets = []
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                at_targets.append(str(comp.qq))
        
        if at_targets:
            # 如果有多个 @，通常机器人的 @ 会在最前面（唤醒词），目标在后面
            # 取最后一个能有效避免识别到机器人
            return at_targets[-1]

        # 从文字提取QQ号（仅在没有@时使用）
        # 注意：为避免与金额等数字混淆，优先选取非金额的5-11位数字
        import re
        candidates = re.findall(r'\b(\d{5,11})\b', event.message_str)
        if not candidates:
            return None
        amount = self._extract_amount(event)
        if amount is not None:
            for token in reversed(candidates):
                if int(token) != amount:
                    return token
        return candidates[-1]

    def _extract_amount(self, event: AstrMessageEvent) -> Optional[int]:
        """从消息中提取金额数字"""
        import re
        at_targets = []
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                at_targets.append(str(comp.qq))
        # 将金额上限从4位提升到8位，以支持更大的贷款和转账
        matches = re.findall(r'\b(\d{1,8})\b', event.message_str)
        for token in matches:
            if token in at_targets:
                continue
            try:
                return int(token)
            except ValueError:
                return None
        return None

    # ==================== 【新增】公寓逻辑 ====================
    def _get_pet_capacity(self, user_data: Dict) -> int:
        """获取用户当前宠物容量上限"""
        # 默认为1个公寓（自带），通过购买公寓增加
        house_count = user_data.get("house_count", 1) 
        
        # 检查租房是否过期
        rented_expiry = user_data.get("rented_house_expiry", 0)
        has_rented = rented_expiry > int(time.time())
        
        rented_bonus = 1 if has_rented else 0
        
        per_house_limit = self.config.get("pet_per_house", 5)
        return (house_count + rented_bonus) * per_house_limit

    async def _check_and_release_excess_pets(self, group_id: str, user_id: str, event: AstrMessageEvent):
        """检查是否超过容量限制，如果是，执行强制放生逻辑"""
        user_data = self._get_user_data(group_id, user_id)
        capacity = self._get_pet_capacity(user_data)
        pets = user_data.get("pets", [])
        
        if len(pets) <= capacity:
            return False, None
            
        # 超出容量，开始强制放生
        excess_count = len(pets) - capacity
        
        # 获取所有宠物详情以计算身价
        pet_details = []
        for pid in pets:
            p_data = self._get_user_data(group_id, pid)
            pet_details.append({
                "id": pid,
                "value": p_data.get("value", 100),
                "nickname": p_data.get("nickname") or f"用户{pid}"
            })
            
        # 按身价排序（降序），保留身价高的，放生身价低的
        pet_details.sort(key=lambda x: x["value"], reverse=True)
        
        kept_pets = pet_details[:capacity]
        released_pets = pet_details[capacity:]
        
        kept_ids = [p["id"] for p in kept_pets]
        user_data["pets"] = kept_ids
        
        total_refund = 0
        release_names = []
        
        for p in released_pets:
            pid = p["id"]
            refund = int(p["value"] * 0.5) # 返还50%
            total_refund += refund
            release_names.append(f"{p['nickname']}({p['value']})")
            
            # 处理被放生的宠物数据
            target_data = self._get_user_data(group_id, pid)
            target_data["master"] = ""
            self._save_user_data(group_id, pid, target_data)
            
        # 返还金币给主人
        user_data["coins"] = user_data.get("coins", 0) + total_refund
        self._save_user_data(group_id, user_id, user_data)
        
        msg = (
            f"🚫 警告：你的公寓容量不足（上限{capacity}只），已强制放生 {excess_count} 只低身价宠物！\n"
            f"🌬️ 离家出走：{', '.join(release_names)}\n"
            f"💰 获得返还：{total_refund} 金币\n"
            f"💡 提示：请使用 /购买公寓 提升容量上限。"
        )
        return True, event.plain_result(msg)
        return True

    async def _fetch_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称（增强版：支持 API 主动获取）"""
        try:
            group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
            if not group_id:
                return f"用户{user_id[-4:]}"

            user_data = self._get_user_data(group_id, user_id)

            # 1. 缓存命中（排除默认占位符）
            cached_nickname = user_data.get("nickname", "")
            if cached_nickname and not cached_nickname.startswith("用户"):
                return cached_nickname

            # 2. 发送者本人：从消息事件获取
            if str(event.get_sender_id()) == user_id:
                sender = event.message_obj.sender
                nickname = getattr(sender, 'card', '') or getattr(sender, 'nickname', '')
                if nickname:
                    user_data["nickname"] = nickname
                    self._save_user_data(group_id, user_id, user_data)
                    return nickname

            # 3. 非发送者：尝试通过 API 获取（aiocqhttp 平台）
            if event.get_platform_name() == "aiocqhttp":
                try:
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                    if isinstance(event, AiocqhttpMessageEvent):
                        client = event.bot
                        info = await client.api.call_action(
                            'get_group_member_info',
                            group_id=int(group_id),
                            user_id=int(user_id),
                            no_cache=False
                        )
                        nickname = info.get('card') or info.get('nickname') or ''
                        if nickname:
                            user_data["nickname"] = nickname
                            self._save_user_data(group_id, user_id, user_data)
                            return nickname
                except Exception as e:
                    logger.debug(f"[宠物市场] API获取昵称失败: {user_id}, {e}")

            # 4. 返回默认昵称
            return f"用户{user_id[-4:]}"

        except Exception as e:
            logger.error(f"[宠物市场] 获取用户昵称异常: {user_id}, {e}")
            return f"用户{user_id[-4:]}"

    def _get_bank_limit(self, level: int) -> int:
        """获取银行存储上限"""
        initial_limit = self.config.get("bank_initial_limit", 1000)
        return int(initial_limit * (1.2 ** (level - 1)))

    def _get_upgrade_cost(self, level: int) -> int:
        """获取银行升级费用"""
        return int(100 * (1.5 ** (level - 1)))

    def _calculate_rob_success_rate(self, attacker_level: int, target_level: int) -> float:
        """计算抢劫成功率（基于银行等级）"""
        base_rate = 0.3
        level_bonus = attacker_level * 0.03
        level_penalty = target_level * 0.02
        success_rate = base_rate + level_bonus - level_penalty
        # 限制在 15% ~ 60%
        return max(0.15, min(0.60, success_rate))

    def _get_evolution_stage(self, value: int) -> str:
        """根据身价获取进化阶段"""
        for stage, config in EVOLUTION_STAGES.items():
            if config["min"] <= value <= config["max"]:
                return stage
        return "普通"

    def _get_evolution_bonuses(self, stage: str) -> Tuple[float, float]:
        """获取进化阶段加成
        Returns:
            (打工加成, 训练加成)
        """
        config = EVOLUTION_STAGES.get(stage, EVOLUTION_STAGES["普通"])
        return config["work_bonus"], config["train_bonus"]

    def _load_template(self, template_path: str) -> str:
        """加载HTML模板"""
        if os.path.exists(template_path):
            try:
                with open(template_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"[宠物市场] 模板加载失败: {e}")
        return "<h1>{{title}}</h1><p>模板加载失败</p>"

    def _calculate_compound_interest(self, principal: int, rate: float, hours: int) -> int:
        """计算复利
        Args:
            principal: 本金
            rate: 每小时利率
            hours: 小时数
        Returns:
            利息金额
        """
        final_amount = principal * ((1 + rate) ** hours)
        interest = int(final_amount - principal)
        return interest

    def _format_amount_change(self, before: int, after: int, label: str = "余额") -> str:
        delta = after - before
        sign = "+" if delta >= 0 else ""
        return f"{label}：{before} -> {after} ({sign}{delta})"

    def _settle_bank_interest(self, user: Dict) -> int:
        bank = user.get("bank", 0)
        now = int(time.time())
        user.setdefault("last_interest_time", now)

        if bank <= 0:
            user["last_interest_time"] = now
            return 0

        last_interest = user.get("last_interest_time", now)
        max_hours = self.config.get("bank_max_interest_time", 24)
        hours = min((now - last_interest) // 3600, max_hours)
        if hours < 1:
            user["last_interest_time"] = now
            return 0

        rate = self.config.get("bank_interest_rate", 0.01)
        interest = self._calculate_compound_interest(bank, rate, hours)
        if interest > 0:
            user["coins"] = user.get("coins", 0) + interest
        user["last_interest_time"] = now
        return interest

    # --- 贷款辅助方法与强制清算逻辑 ---
    def _update_loan_interest(self, user_data: Dict) -> int:
        """更新用户的贷款利息（带封顶逻辑）"""
        loan_total = user_data.get("loan_amount", 0)
        principal = user_data.get("loan_principal", 0)

        # 如果没有贷款或利息被冻结（坏账），不计算利息
        if loan_total <= 0 or user_data.get("loan_interest_frozen", False):
            user_data["last_loan_interest_time"] = int(time.time())
            if loan_total <= 0:
                user_data["loan_principal"] = 0  # 欠款没了，本金也清零
                user_data["loan_interest_frozen"] = False
            return 0

        rate = self.config.get("loan_interest_rate", 0.05)
        # 获取利息上限倍率（默认 1.0，即利息最多等于本金）
        max_multiplier = self.config.get("loan_interest_max_multiplier", 1.0)

        last_time = user_data.get("last_loan_interest_time", int(time.time()))
        now = int(time.time())
        hours = (now - last_time) // 3600

        if hours >= 1:
            # 1. 计算理论上的复利后总金额（避免溢出）
            theoretical_loan = loan_total * ((1 + rate) ** hours)
            if not math.isfinite(theoretical_loan):
                theoretical_loan = loan_total
            theoretical_loan = int(theoretical_loan)

            # 2. 如果倍率 <= 0，仅关闭清算，不对利息封顶
            if max_multiplier <= 0:
                new_loan = theoretical_loan
            else:
                # 计算封顶金额 = 本金 + 本金*倍率
                max_loan = int(principal * (1 + max_multiplier))
                # 比较，取较小值
                if principal > 0:
                    new_loan = min(theoretical_loan, max_loan)
                else:
                    new_loan = theoretical_loan

            interest_added = new_loan - loan_total
            if interest_added > 0:
                user_data["loan_amount"] = new_loan

            user_data["last_loan_interest_time"] = now
            return interest_added

        return 0

    # --- 投资相关辅助方法 ---
    def _get_investment_trend(self) -> Tuple[int, float]:
        """
        生成投资趋势
        主投资分布：1(40%) 2(25%) 3(20%) 4(8%) 5(5%) 6(1.5%) 7(0.5%)
        加投分布：1(50%) 2(25%) 3(15%) 4(7%) 5(2.5%) 6(0.4%) 7(0.1%)
        返回：(趋势类型, 涨跌百分比)
        """
        rand = random.random() * 100
        
        # 趋势分布及其涨跌范围
        # (概率范围, 趋势名, 涨跌范围)
        trends = [
            ((0, 40), "横盘", lambda: random.uniform(-0.02, 0.02)),           # 1
            ((40, 65), "小涨", lambda: random.uniform(0.03, 0.05)),           # 2
            ((65, 85), "小跌", lambda: random.uniform(-0.04, -0.03)),         # 3
            ((85, 93), "中涨", lambda: random.uniform(0.06, 0.09)),           # 4
            ((93, 98), "中跌", lambda: random.uniform(-0.091, -0.05)),        # 5
            ((98, 99.5), "极端涨", lambda: random.uniform(0.10, 0.15)),       # 6
            ((99.5, 100), "极端跌", lambda: random.uniform(-0.18, -0.10)),    # 7
        ]
        
        for (min_p, max_p), name, func in trends:
            if min_p <= rand < max_p:
                return (name, func())
        
        return ("横盘", random.uniform(-0.02, 0.02))

    def _get_investment_trend_addon(self) -> Tuple[int, float]:
        """
        生成加投趋势
        加投分布：1(50%) 2(25%) 3(15%) 4(7%) 5(2.5%) 6(0.4%) 7(0.1%)
        """
        rand = random.random() * 100
        
        trends = [
            ((0, 50), "横盘", lambda: random.uniform(-0.01, 0.01)),           # 1
            ((50, 75), "小涨", lambda: random.uniform(0.02, 0.04)),           # 2
            ((75, 90), "小跌", lambda: random.uniform(-0.039, -0.02)),        # 3
            ((90, 97), "中涨", lambda: random.uniform(0.05, 0.09)),           # 4
            ((97, 99.5), "中跌", lambda: random.uniform(-0.05, -0.04)),       # 5
            ((99.5, 99.9), "极端涨", lambda: random.uniform(0.10, 0.12)),     # 6
            ((99.9, 100), "极端跌", lambda: random.uniform(-0.081, -0.051)),  # 7
        ]
        
        for (min_p, max_p), name, func in trends:
            if min_p <= rand < max_p:
                return (name, func())
        
        return ("横盘", random.uniform(-0.01, 0.01))

    def _check_investment_trigger(self, investment: Dict) -> Optional[str]:
        """
        检查投资是否触发止盈或止损
        返回：None（无触发） | "止盈" | "止损"
        """
        # 【修复】使用总投资额（包含加投）来计算收益率
        total_input = investment["amount"] + investment.get("addon_amount", 0)
        if total_input <= 0:
            return None
        
        profit_rate = (investment["current_value"] - total_input) / total_input
        
        # 止盈条件：盈利达10%
        if profit_rate >= 0.10:
            return "止盈"
        
        # 止损条件：亏损达5%
        if profit_rate <= -0.05:
            return "止损"
        
        return None

    def _settle_investments(self, user_data: Dict) -> List[str]:
        """
        自动结算投资趋势变化（每次操作时调用）
        返回结算信息列表
        """
        messages = []
        investments = user_data.get("investments", [])
        
        for investment in investments:
            if investment.get("status") != "active":
                continue
            
            # 检查是否达到结算时间（每小时结算一次）
            next_settlement = investment.get("next_settlement_time", 0)
            now = int(time.time())
            
            if now >= next_settlement:
                # 【修复】根据是否有加投金额来选择趋势函数
                addon_amount = investment.get("addon_amount", 0)
                if addon_amount > 0:
                    # 有加投，使用加投趋势
                    trend_name, change_rate = self._get_investment_trend_addon()
                else:
                    # 纯主投资，使用主投资趋势
                    trend_name, change_rate = self._get_investment_trend()
                
                # 更新投资价值
                old_value = investment["current_value"]
                new_value = int(old_value * (1 + change_rate))
                investment["current_value"] = new_value
                investment["trend_history"].append((trend_name, change_rate))
                investment["next_settlement_time"] = now + 3600
                
                # 检查触发条件
                trigger = self._check_investment_trigger(investment)
                if trigger:
                    total_input = investment["amount"] + addon_amount
                    profit_loss = new_value - total_input
                    # 【改进】消息格式更加清晰，区分盈利和亏损
                    if profit_loss >= 0:
                        messages.append(f"🔔 你的投资触发{trigger}条件！收益：{profit_loss:+d}金币，建议使用 /{trigger}")
                    else:
                        messages.append(f"🔔 你的投资触发{trigger}条件！亏损：{profit_loss:+d}金币，建议使用 /{trigger}")
                else:
                    messages.append(f"📊 投资更新：{trend_name} {change_rate:+.2%}，当前价值 {new_value} 金币")
        
        return messages

    def _get_loan_limit(self, level: int) -> int:
        """根据银行等级获取贷款额度"""
        per_level = self.config.get("loan_limit_per_level", 5000)
        return level * per_level

    async def _check_and_liquidate(self, event: AstrMessageEvent, group_id: str, user_id: str, user_data: Dict) -> bool:
        """
        【新增】检查并执行强制清算 (防老赖机制)
        Returns: 是否触发了清算
        """
        principal = user_data.get("loan_principal", 0)
        loan = user_data.get("loan_amount", 0)
        multiplier = self.config.get("loan_interest_max_multiplier", 1.0)

        # 0 表示关闭此功能，或者没贷款
        if multiplier <= 0 or loan <= 0:
            return False

        # 如果已经是冻结状态，不再重复清算
        if user_data.get("loan_interest_frozen", False):
            return False

        # 爆仓阈值：本金 * (1 + 倍率)
        threshold = int(principal * (1 + multiplier))

        # 未达到爆仓线
        if loan < threshold:
            return False

        # === 触发强制清算 ===
        log_msg = ["🛑 【银行强制执行通知】"]
        log_msg.append(f"您的欠款 ({loan}) 已达到本金的 {1 + multiplier} 倍！")
        log_msg.append("银行依法启动资产强制清算程序...")

        total_repay = 0

        # 1. 现金强制划扣 (低保上限 1000)
        current_coins = user_data.get("coins", 0)
        safe_limit = 1000  # 低保上限
        if current_coins > safe_limit:
            # 计算可以划扣的金额
            force_deduct = current_coins - safe_limit
            # 最多只需要还清欠款
            actual_deduct = min(force_deduct, loan)

            if actual_deduct > 0:
                user_data["coins"] -= actual_deduct
                total_repay += actual_deduct
                log_msg.append(f"🔻 强制划扣现金（超限部分）：{actual_deduct} 金币")

        # 2. 划扣银行存款
        remaining_debt_1 = loan - total_repay
        if remaining_debt_1 > 0:
            bank_balance = user_data.get("bank", 0)
            if bank_balance > 0:
                deduct = min(bank_balance, remaining_debt_1)
                user_data["bank"] -= deduct
                total_repay += deduct
                log_msg.append(f"🔻 划扣银行存款：{deduct} 金币")

        # 3. 变卖宠物 (8折)
        remaining_debt_2 = loan - total_repay
        if remaining_debt_2 > 0:
            pets = user_data.get("pets", [])
            if pets:
                sold_count = 0
                pets_income = 0
                # 复制列表进行遍历
                for pet_id in list(pets):
                    # 如果钱够了就不卖了
                    if pets_income >= remaining_debt_2:
                        break

                    pet = self._get_user_data(group_id, pet_id)
                    market_value = int(pet.get("value", 100) * 0.8)  # 8折

                    pet["master"] = ""  # 解除关系
                    pets_income += market_value
                    sold_count += 1
                    user_data["pets"].remove(pet_id)
                    self._save_user_data(group_id, pet_id, pet)

                total_repay += pets_income
                log_msg.append(f"🔻 强制拍卖 {sold_count} 只宠物，获得 {pets_income} 金币")

        # 4. 执行还款
        user_data["loan_amount"] = max(0, loan - total_repay)

        # 5. 结算状态
        if user_data["loan_amount"] > 0:
            # 【新增】追缴转账资金
            suspicious_transfers = user_data.get("loan_transfers", [])
            if suspicious_transfers:
                clawback_count = 0
                for record in suspicious_transfers:
                    # 将追缴任务加入队列，由后台任务异步处理
                    self.debt_queue.append({
                        "group_id": group_id,
                        "debtor_id": user_id,
                        "target_id": record["target"],
                        "amount": record["amount"],
                        "target_name": record.get("target_name", record["target"])
                    })
                    clawback_count += 1
                
                # 清空记录防止重复追缴
                user_data["loan_transfers"] = []
                log_msg.append(f"🕵️ 发现 {clawback_count} 笔存续期间的转账记录。")
                log_msg.append("⚖️ 银行已启动外部资金追回程序，将从收款人账户强制划扣！")

            # 依然资不抵债
            user_data["loan_interest_frozen"] = True
            log_msg.append(f"⚠️ 资产抵扣后仍欠款 {user_data['loan_amount']} 金币。")
            log_msg.append("❄️ 剩余欠款利息已冻结，不再增加。")
            log_msg.append("🛡️ 请尽快打工还清剩余债务！")
        else:
            # 还清了
            user_data["loan_principal"] = 0
            user_data["loan_interest_frozen"] = False
            remaining_cash = abs(loan - total_repay)  # 如果有剩余 (通常是宠物卖多了)
            if remaining_cash > 0:
                user_data["coins"] = user_data.get("coins", 0) + remaining_cash
                log_msg.append(f"✅ 债务已结清！资产剩余 {remaining_cash} 金币退回余额。")
            else:
                log_msg.append(f"✅ 债务已结清！")

        # 6. 低保机制补齐
        # 防止用户彻底无法翻身
        current_coins = user_data.get("coins", 0)
        if current_coins < INITIAL_COINS:
            subsidy = INITIAL_COINS - current_coins
            user_data["coins"] = INITIAL_COINS
            log_msg.append(f"🎁 【失业救济金】发放低保 {subsidy} 金币，助力重新开始。")

        self._save_user_data(group_id, user_id, user_data)

        # 发送通知
        await event.send(MessageChain([star.Plain("\n".join(log_msg))]))
        return True

    # ==================== 命令：宠物菜单 ====================
    @filter.command("宠物菜单")
    async def pet_menu(self, event: AstrMessageEvent):
        """显示功能菜单"""
        menu_data = {
            "title": "🐾 宠物市场菜单",
            "items": [
                {"cmd": "/宠物市场 [页码]", "desc": "查看群内宠物列表（支持分页）"},
                {"cmd": "/购买宠物 @群友/QQ", "desc": "购买指定宠物"},
                {"cmd": "/放生宠物 @群友/QQ", "desc": "放生宠物（返还30%身价）"},
                {"cmd": "/赎身", "desc": "🎉 宠物赎身获得自由（24小时保护期）"},
                {"cmd": "/购买公寓", "desc": "🏠 购买公寓增加宠物容量上限"},
                {"cmd": "/租房", "desc": "📅 租借临时公寓(+5容量/7天)"},
                {"cmd": "/我的公寓 [编号]", "desc": "🏘️ 查看公寓与入住情况"},
                {"cmd": "/宠物签到", "desc": "📅 每日签到领工资"},
                {"cmd": "/福利彩票 [机选/号码]", "desc": "🎰 双色球彩票，以小博大"},
                {"cmd": "/商店", "desc": "🛒 购买道具增强体验"},
                {"cmd": "/购买道具 [ID]", "desc": "💳 购买指定道具"},
                {"cmd": "/我的背包", "desc": "🎒 查看和使用道具"},
                {"cmd": "/使用道具 [ID]", "desc": "🧪 使用背包物品"},
                {"cmd": "/打工", "desc": "派遣所有宠物打工赚钱"},
                {"cmd": "/逃跑", "desc": "尝试逃离主人(30%成功)"},
                {"cmd": "/训练 @群友/QQ", "desc": "训练单只宠物提升身价（冷却1天）"},
                {"cmd": "/一键训练", "desc": "📚 批量训练所有宠物"},
                {"cmd": "/进化宠物 @群友/QQ", "desc": "消耗金币进化宠物阶段"},
                {"cmd": "/PK @群友/QQ", "desc": "⚔️ 宠物决斗（赢家掠夺10%身价）"},
                {"cmd": "/我的宠物", "desc": "查看自己的宠物与金币"},
                {"cmd": "/银行信息", "desc": "查看银行等级与利息"},
                {"cmd": "/升级信用", "desc": "提升银行等级与存储上限"},
                {"cmd": "/领取利息", "desc": "领取银行存款利息到余额"},
                {"cmd": "/存款 100", "desc": "存入金币到银行"},
                {"cmd": "/取款 50", "desc": "从银行取出金币"},
                {"cmd": "/贷款 500", "desc": "💸 向银行借款（需支付利息）"},
                {"cmd": "/还款 [金额]", "desc": "💳 偿还欠款（不填则还清）"},
                {"cmd": "/转账 @群友/QQ 金额", "desc": "转账给其他玩家"},
                {"cmd": "/转账记录", "desc": "查看最近10条转账记录"},
                {"cmd": "/宠物身价排行榜 [页码]", "desc": "查看身价排行（支持分页）"},
                {"cmd": "/宠物资金排行榜 [页码]", "desc": "查看余额排行（支持分页）"},
                {"cmd": "/群内十大首富 [页码]", "desc": "查看总资产排行（支持分页）"},
                {"cmd": "/抢劫 @群友/QQ", "desc": "每小时可抢劫一次"},
                {"cmd": "/交罚款", "desc": "抢劫失败后缴纳罚款"},
                {"cmd": "/坐牢", "desc": "抢劫失败后选择坐牢"},
                {"cmd": "/金融市场", "desc": "📊 查看基金/股票/虚拟币大盘"},
                {"cmd": "/买入 [代码] [金额]", "desc": "💸 购买理财产品"},
                {"cmd": "/卖出 [代码] [全部/金额]", "desc": "💰 卖出持仓变现"},
                {"cmd": "/金融帮助", "desc": "📘 查看金融市场操作指南"},
                {"cmd": "/我的持仓", "desc": "👜 查看持仓详情与盈亏"},
            ]
        }
        try:
            template = self._load_template(MENU_TEMPLATE)
            url = await self.html_render(template, menu_data)
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[宠物市场] 菜单图片生成失败: {e}，使用纯文本兜底")
            # 兜底方案：使用纯文本菜单
            text_menu = "🐾 宠物市场菜单\n\n"
            item_texts = [f"{item['cmd']} {item['desc']}" for item in menu_data["items"]]
            col_width = max(len(t) for t in item_texts) + 4 if item_texts else 0
            for i in range(0, len(item_texts), 2):
                left = item_texts[i]
                right = item_texts[i + 1] if i + 1 < len(item_texts) else ""
                text_menu += f"{left.ljust(col_width)}{right}\n"
            text_menu += "\n💡 提示：图片菜单生成失败，显示文本版本"
            yield event.plain_result(text_menu)

    #
    # ==================== 命令：宠物市场 ====================
    @filter.command("宠物市场")
    async def pet_list(self, event: AstrMessageEvent, page: int = 1):
        """查看群内宠物列表（支持分页）"""
        # 私聊检测
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此功能仅限群聊使用。")
            return

        group_id = str(event.message_obj.group_id)
        pets = self._get_pets_in_group(group_id)
        if not pets:
            yield event.plain_result("本群暂无宠物数据。")
            return

        # 分页逻辑
        page_size = 20
        total = len(pets)
        total_pages = (total + page_size - 1) // page_size
        page = max(1, min(page, total_pages))  # 限制页码范围
        start = (page - 1) * page_size
        end = start + page_size

        lines = [f"【🐾 宠物市场】第 {page}/{total_pages} 页"]
        for uid, data in list(pets.items())[start:end]:
            name = data.get("nickname") or await self._fetch_nickname(event, uid)
            value = data.get("value", 100)
            master = data.get("master", "")
            stage = data.get("evolution_stage", "普通")
            # 主人显示为昵称而非 QQ 号
            if not master:
                status = "🆓 自由"
            else:
                master_name = await self._fetch_nickname(event, master)
                status = f"👤 属于{master_name}"
            lines.append(f"[{stage}] {name} | 💰{value} | {status}")

        if total_pages > 1:
            lines.append(f"\n💡 发送 /宠物市场 {page + 1 if page < total_pages else 1} 查看其他页")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：购买宠物 ====================
    @filter.command("购买宠物")
    async def purchase_pet(self, event: AstrMessageEvent):
        """购买宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定要购买的宠物。")
            return

        if target_id == user_id:
            yield event.plain_result("❌ 不能购买自己。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        # 使用交易锁（按ID排序避免死锁）
        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                target_data = self._get_user_data(group_id, target_id)

                # 检查是否为宠物尝试购买主人
                buyer_master = user_data.get("master", "")
                if buyer_master and target_id == buyer_master:
                    yield event.plain_result("❌ 你不能购买自己的主人！")
                    return

                # 检查目标是否在保护期（赎身后24小时）
                protection_until = target_data.get("protection_until", 0)
                if int(time.time()) < protection_until:
                    remain = protection_until - int(time.time())
                    hours = remain // 3600
                    mins = (remain % 3600) // 60
                    target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
                    yield event.plain_result(f"❌ {target_name} 正处于保护期，{hours}小时{mins}分钟后才能被购买。")
                    return

                # 检查冷却
                cooldown_seconds = self.config.get("purchase_cooldown", 3600)
                in_cooldown, remain = self._check_cooldown(user_data, "purchase", cooldown_seconds)
                if in_cooldown:
                    mins = remain // 60
                    secs = remain % 60
                    yield event.plain_result(f"⏰ 购买冷却中，剩余 {mins}分{secs}秒。")
                    return
                
                # 【新增】检查公寓容量
                capacity = self._get_pet_capacity(user_data)
                current_pets = len(user_data.get("pets", []))
                if current_pets >= capacity:
                    yield event.plain_result(f"❌ 你的公寓已满（{current_pets}/{capacity}）！请先购买更多公寓。")
                    return

                # 检查是否已拥有
                if target_id in user_data.get("pets", []):
                    mins = remain // 60
                    secs = remain % 60
                    yield event.plain_result(f"⏰ 购买冷却中，剩余 {mins}分{secs}秒。")
                    return

                # 检查是否已拥有
                if target_id in user_data.get("pets", []):
                    yield event.plain_result("❌ 该宠物已经是你的了。")
                    return

                # 双重检查宠物归属
                current_master = target_data.get("master", "")
                if current_master == user_id:
                    yield event.plain_result("❌ 该宠物已经是你的了。")
                    return

                cost = target_data.get("value", 100)
                if user_data.get("coins", 0) < cost:
                    yield event.plain_result(f"❌ 金币不足，需要 {cost} 金币。")
                    return

                # 执行购买
                coins_before = user_data.get("coins", 0)
                user_data["coins"] -= cost
                user_data.setdefault("pets", []).append(target_id)
                self._set_cooldown(user_data, "purchase")

                old_master = target_data.get("master", "")
                value_increase = random.randint(10, 30)
                target_data["value"] += value_increase
                target_data["master"] = user_id

                # 更新进化阶段
                target_data["evolution_stage"] = self._get_evolution_stage(target_data["value"])

                if not old_master:
                    # 无主人：50% 补贴给宠物
                    subsidy = cost // 2
                    target_data["coins"] = target_data.get("coins", 0) + subsidy
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)
                    target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
                    yield event.plain_result(
                        f"✅ 成功购买宠物 {target_name}，消耗 {cost} 金币。\n"
                        f"💰 宠物身价上涨 {value_increase}，获得补贴 {subsidy} 金币。\n"
                        f"⭐ 当前阶段：{target_data['evolution_stage']}\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )
                else:
                    # 有主人：原主人获得全额
                    old_master_data = self._get_user_data(group_id, old_master)
                    old_master_data["coins"] = old_master_data.get("coins", 0) + cost
                    if target_id in old_master_data.get("pets", []):
                        old_master_data["pets"].remove(target_id)
                    self._save_user_data(group_id, old_master, old_master_data)
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)
                    old_name = old_master_data.get("nickname") or await self._fetch_nickname(event, old_master)
                    target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
                    yield event.plain_result(
                        f"✅ 成功从 {old_name} 手中购买宠物 {target_name}，消耗 {cost} 金币。\n"
                        f"💵 原主人获得 {cost} 金币，宠物身价上涨 {value_increase}。\n"
                        f"⭐ 当前阶段：{target_data['evolution_stage']}\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )

    # ==================== 命令：放生宠物 ====================
    @filter.command("放生宠物")
    async def release_pet(self, event: AstrMessageEvent):
        """放生宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定要放生的宠物。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            if target_id not in user_data.get("pets", []):
                yield event.plain_result("❌ 该宠物不在你的列表中。")
                return

            # 检查冷却
            cooldown_seconds = self.config.get("release_cooldown", 3600)
            in_cooldown, remain = self._check_cooldown(user_data, "release", cooldown_seconds)
            if in_cooldown:
                mins = remain // 60
                yield event.plain_result(f"⏰ 放生冷却中，剩余 {mins} 分钟。")
                return

            target_data = self._get_user_data(group_id, target_id)
            target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
            pet_value = target_data.get("value", 100)

            # 返还30%价值给主人
            coins_before = user_data.get("coins", 0)
            refund = int(pet_value * 0.3)
            user_data["coins"] = user_data.get("coins", 0) + refund

            user_data["pets"].remove(target_id)
            target_data["master"] = ""
            self._set_cooldown(user_data, "release")
            self._save_user_data(group_id, user_id, user_data)
            self._save_user_data(group_id, target_id, target_data)
            yield event.plain_result(
                f"🕊️ 成功放生宠物 {target_name}！\n"
                f"💰 返还 {refund} 金币（身价30%）\n"
                f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
            )

    # ==================== 命令：打工 ====================
    @filter.command("打工")
    async def work(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            cooldown_seconds = self.config.get("work_cooldown", 3600)
            in_cooldown, remain = self._check_cooldown(user_data, "work", cooldown_seconds)

            if in_cooldown:
                mins = remain // 60
                secs = remain % 60
                yield event.plain_result(f"⏰ 打工冷却中，剩余 {mins}分{secs}秒。")
                return

            coins_before = user_data.get("coins", 0)

            pets = user_data.get("pets", [])
            total = 0
            lines = ["【💼 打工报告】"]

            if not pets:
                income = random.randint(10, 50)
                total += income
                lines.append(f"你没有宠物，只能自己去打工，赚了 {income} 金币。")
            else:
                for pid in pets:
                    pet = self._get_user_data(group_id, pid)
                    name = pet.get("nickname") or await self._fetch_nickname(event, pid)
                    stage = pet.get("evolution_stage", "普通")
                    work_bonus, _ = self._get_evolution_bonuses(stage)

                    if random.random() < 0.8:
                        base_income = random.randint(20, 80) + pet.get("value", 100) // 10
                        income = int(base_income * (1 + work_bonus))
                        total += income
                        copywriting = random.choice(self.copywriting.get("success", ["打工成功！"]))
                        lines.append(f"[{stage}] {name}：{copywriting} +{income}")
                    else:
                        loss = random.randint(10, 30)
                        pet["value"] = max(100, pet["value"] - loss)
                        pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                        copywriting = random.choice(self.copywriting.get("failure", ["打工失败..."]))
                        lines.append(f"[{stage}] {name}：{copywriting} 身价-{loss} (当前{pet['value']})")
                        self._save_user_data(group_id, pid, pet)

            # 【新增】打工纳税逻辑
            master_id = user_data.get("master", "")
            tax_rate = self.config.get("work_tax_rate", 0.3)

            if master_id and total > 0:
                tax = int(total * tax_rate)
                net_income = total - tax

                # 给主人加钱
                master_data = self._get_user_data(group_id, master_id)
                master_before = master_data.get("coins", 0)
                master_data["coins"] = master_data.get("coins", 0) + tax
                self._save_user_data(group_id, master_id, master_data)

                master_name = master_data.get("nickname") or await self._fetch_nickname(event, master_id)

                user_data["coins"] = user_data.get("coins", 0) + net_income
                lines.append(f"\n💸 上交主人({master_name}) {int(tax_rate * 100)}%：{tax} 金币")
                lines.append(f"💰 实得收入：{net_income} 金币")
                lines.append(self._format_amount_change(master_before, master_data["coins"], f"👑 主人({master_name})余额"))
            else:
                user_data["coins"] = user_data.get("coins", 0) + total
                lines.append(f"\n💰 总计获得 {total} 金币")

            
            self._set_cooldown(user_data, "work")
            self._save_user_data(group_id, user_id, user_data)

            lines.append(self._format_amount_change(coins_before, user_data["coins"], "💵 余额"))
            
            yield event.plain_result("\n".join(lines))

    # ==================== 【新增】命令：逃跑 ====================
    @filter.command("逃跑")
    async def escape(self, event: AstrMessageEvent):
        """宠物尝试逃跑"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 你还在监狱中，没法越狱。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            master_id = user_data.get("master", "")

            if not master_id:
                yield event.plain_result("❌ 你是自由之身，无需逃跑。")
                return

            # 检查冷却 (共用赎身冷却或单独设置，这里简单复用赎身逻辑相关的保护期概念，或者给逃跑单独加个冷却防止刷屏)
            # 这里简单起见，使用 work_cooldown 防止无限刷
            cooldown_seconds = 300
            in_cooldown, remain = self._check_cooldown(user_data, "escape", cooldown_seconds)
            if in_cooldown:
                yield event.plain_result(f"🏃 刚跑累了，休息 {remain} 秒后再试。")
                return
            self._set_cooldown(user_data, "escape")

            success_rate = self.config.get("escape_success_rate", 0.3)

            if random.random() < success_rate:
                # 成功
                user_data["master"] = ""
                # 从主人列表移除
                master_data = self._get_user_data(group_id, master_id)
                if user_id in master_data.get("pets", []):
                    master_data["pets"].remove(user_id)
                self._save_user_data(group_id, master_id, master_data)

                # 保护期
                protection_hours = self.config.get("ransom_protection_hours", 24)
                user_data["protection_until"] = int(time.time()) + (protection_hours * 3600)

                self._save_user_data(group_id, user_id, user_data)
                yield event.plain_result(f"🎉 逃跑成功！你重获自由，并获得 {protection_hours} 小时保护期！")
            else:
                # 失败：负债翻倍
                # 如果没有负债，则增加一笔等同于身价的负债作为惩罚
                current_loan = user_data.get("loan_amount", 0)
                penalty = 0
                if current_loan > 0:
                    penalty = current_loan  # 翻倍即再加一倍
                    user_data["loan_amount"] += penalty
                    user_data["loan_principal"] += penalty
                else:
                    # 无债逃跑失败，背负身价债务
                    pet_value = user_data.get("value", 100)
                    penalty = pet_value
                    user_data["loan_amount"] = penalty
                    user_data["loan_principal"] = penalty

                self._save_user_data(group_id, user_id, user_data)
                yield event.plain_result(
                    f"💔 逃跑失败！被抓回来了...\n📉 惩罚：负债增加 {penalty} 金币！\n💸 当前欠款：{user_data['loan_amount']}")

    # ==================== 命令：训练 ====================
    @filter.command("训练")
    async def train_pet(self, event: AstrMessageEvent):
        """训练宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定要训练的宠物。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                if target_id not in user_data.get("pets", []):
                    yield event.plain_result("❌ 该宠物不在你的列表中。")
                    return

                pet = self._get_user_data(group_id, target_id)
                cooldown_seconds = self.config.get("train_cooldown", 86400)
                in_cooldown, remain = self._check_cooldown(pet, "train", cooldown_seconds)

                if in_cooldown:
                    hours = remain // 3600
                    mins = (remain % 3600) // 60
                    yield event.plain_result(f"⏰ 宠物训练冷却中，剩余 {hours}小时{mins}分钟。")
                    return

                coins_before = user_data.get("coins", 0)
                cost = int(pet["value"] * self.config.get("train_cost_rate", 0.1))
                if user_data.get("coins", 0) < cost:
                    yield event.plain_result(f"❌ 金币不足，训练需要 {cost} 金币。")
                    return

                user_data["coins"] -= cost

                # 获取进化加成
                stage = pet.get("evolution_stage", "普通")
                _, train_bonus = self._get_evolution_bonuses(stage)
                success_rate = self.config.get("train_success_rate", 0.7) + train_bonus

                if random.random() < success_rate:
                    # 训练成功：混合模式
                    base_increase = random.randint(15, 35)
                    rate_increase = int(pet["value"] * 0.1)
                    increase = base_increase + rate_increase
                    pet["value"] += increase
                    pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                    self._set_cooldown(pet, "train")
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, pet)
                    name = pet.get("nickname") or await self._fetch_nickname(event, target_id)
                    msg = random.choice(self.train_copywriting.get("success", [
                        "{name} 训练成功，身价提升 {increase}，当前 {value} 金币。"
                    ])).format(name=name, increase=increase, value=pet["value"])
                    yield event.plain_result(
                        f"✅ {msg}\n"
                        f"⭐ 当前阶段：{pet['evolution_stage']}\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )
                else:
                    # 训练失败
                    decrease = random.randint(10, 25)
                    pet["value"] = max(100, pet["value"] - decrease)
                    pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                    self._set_cooldown(pet, "train")
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, pet)
                    name = pet.get("nickname") or await self._fetch_nickname(event, target_id)
                    msg = random.choice(self.train_copywriting.get("failure", [
                        "{name} 训练失败，身价下降 {decrease}，当前 {value} 金币。"
                    ])).format(name=name, decrease=decrease, value=pet["value"])
                    yield event.plain_result(
                        f"❌ {msg}\n"
                        f"⭐ 当前阶段：{pet['evolution_stage']}\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )

    # ==================== 命令：赎身 ====================
    @filter.command("赎身")
    async def ransom(self, event: AstrMessageEvent):
        """宠物赎身"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            master_id = user_data.get("master", "")

            if not master_id:
                yield event.plain_result("❌ 你是自由之身，无需赎身。")
                return

            pet_value = user_data.get("value", 100)
            if user_data.get("coins", 0) < pet_value:
                yield event.plain_result(f"❌ 金币不足，赎身需要 {pet_value} 金币（你的身价）。")
                return

            # 扣除金币，支付给主人
            coins_before = user_data.get("coins", 0)
            user_data["coins"] -= pet_value
            master_data = self._get_user_data(group_id, master_id)
            master_data["coins"] = master_data.get("coins", 0) + pet_value
            if user_id in master_data.get("pets", []):
                master_data["pets"].remove(user_id)

            # 解除主从关系
            user_data["master"] = ""

            # 设置保护期（24小时）
            protection_hours = self.config.get("ransom_protection_hours", 24)
            user_data["protection_until"] = int(time.time()) + (protection_hours * 3600)

            self._save_user_data(group_id, user_id, user_data)
            self._save_user_data(group_id, master_id, master_data)

            user_name = user_data.get("nickname") or await self._fetch_nickname(event, user_id)
            master_name = master_data.get("nickname") or await self._fetch_nickname(event, master_id)

            yield event.plain_result(
                f"🎉 赎身成功！{user_name} 重获自由！\n"
                f"💰 支付 {pet_value} 金币给 {master_name}\n"
                f"🛡️ 获得 {protection_hours} 小时保护期\n"
                f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
            )

    # ==================== 命令：一键训练 ====================
    @filter.command("一键训练", alias={"批量训练"})
    async def batch_train(self, event: AstrMessageEvent):
        """一键训练所有宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            coins_before = user_data.get("coins", 0)
            pets = user_data.get("pets", [])

            if not pets:
                yield event.plain_result("❌ 你还没有宠物，无法训练。")
                return

            # 统计数据
            total_cost = 0
            success_count = 0
            fail_count = 0
            cooldown_count = 0
            results = []

            for pet_id in pets:
                pet = self._get_user_data(group_id, pet_id)
                cooldown_seconds = self.config.get("train_cooldown", 86400)
                in_cooldown, _ = self._check_cooldown(pet, "train", cooldown_seconds)

                if in_cooldown:
                    cooldown_count += 1
                    continue

                cost = int(pet["value"] * self.config.get("train_cost_rate", 0.1))
                if user_data.get("coins", 0) < cost:
                    # 金币不足，停止训练
                    break

                user_data["coins"] -= cost
                total_cost += cost

                # 获取进化加成
                stage = pet.get("evolution_stage", "普通")
                _, train_bonus = self._get_evolution_bonuses(stage)
                success_rate = self.config.get("train_success_rate", 0.7) + train_bonus

                name = pet.get("nickname") or await self._fetch_nickname(event, pet_id)

                if random.random() < success_rate:
                    # 训练成功
                    base_increase = random.randint(15, 35)
                    rate_increase = int(pet["value"] * 0.1)
                    increase = base_increase + rate_increase
                    pet["value"] += increase
                    pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                    self._set_cooldown(pet, "train")
                    self._save_user_data(group_id, pet_id, pet)
                    success_count += 1
                    results.append(f"✅ {name}: +{increase} ({pet['value']})")
                else:
                    # 训练失败
                    decrease = random.randint(10, 25)
                    pet["value"] = max(100, pet["value"] - decrease)
                    pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                    self._set_cooldown(pet, "train")
                    self._save_user_data(group_id, pet_id, pet)
                    fail_count += 1
                    results.append(f"❌ {name}: -{decrease} ({pet['value']})")

            self._save_user_data(group_id, user_id, user_data)

            # 输出结果
            summary = f"【📚 批量训练报告】\n"
            summary += f"成功：{success_count} | 失败：{fail_count} | 冷却：{cooldown_count}\n"
            summary += f"💰 总消耗：{total_cost} 金币\n"
            summary += f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}\n\n"
            summary += "\n".join(results[:10])  # 只显示前10条

            if len(results) > 10:
                summary += f"\n... 还有 {len(results) - 10} 只宠物"

            yield event.plain_result(summary)

    # ==================== 命令：进化宠物 ====================
    @filter.command("进化宠物")
    async def evolve_pet(self, event: AstrMessageEvent):
        """进化宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定要进化的宠物。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                if target_id not in user_data.get("pets", []):
                    yield event.plain_result("❌ 该宠物不在你的列表中。")
                    return

                pet = self._get_user_data(group_id, target_id)
                current_stage = pet.get("evolution_stage", "普通")
                pet_value = pet.get("value", 100)
                name = pet.get("nickname") or await self._fetch_nickname(event, target_id)

                # 判断能否进化
                if current_stage == "普通":
                    if pet_value < 500:
                        yield event.plain_result(f"❌ {name} 身价不足500，无法进化到稀有阶段。")
                        return
                    next_stage = "稀有"
                    cost = EVOLUTION_COSTS["稀有"]
                elif current_stage == "稀有":
                    if pet_value < 2000:
                        yield event.plain_result(f"❌ {name} 身价不足2000，无法进化到史诗阶段。")
                        return
                    next_stage = "史诗"
                    cost = EVOLUTION_COSTS["史诗"]
                elif current_stage == "史诗":
                    if pet_value < 5000:
                        yield event.plain_result(f"❌ {name} 身价不足5000，无法进化到传说阶段。")
                        return
                    next_stage = "传说"
                    cost = 5000  # 传说进化消耗
                elif current_stage == "传说":
                    yield event.plain_result(f"🌟 {name} 已是传说阶段，无法继续进化！")
                    return

                # 检查金币
                if user_data.get("coins", 0) < cost:
                    yield event.plain_result(f"❌ 金币不足，进化需要 {cost} 金币。")
                    return

                # 执行进化（20%失败率）
                coins_before = user_data.get("coins", 0)
                user_data["coins"] -= cost
                if random.random() < 0.8:  # 80%成功率
                    pet["evolution_stage"] = next_stage
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, pet)
                    work_bonus, train_bonus = self._get_evolution_bonuses(next_stage)
                    yield event.plain_result(
                        f"🎉 进化成功！{name} 进化到 [{next_stage}] 阶段！\n"
                        f"💰 消耗 {cost} 金币\n"
                        f"📈 打工收益 +{int(work_bonus * 100)}%\n"
                        f"📈 训练成功率 +{int(train_bonus * 100)}%\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )
                else:
                    # 进化失败，身价-10%
                    loss = int(pet_value * 0.1)
                    pet["value"] = max(100, pet["value"] - loss)
                    pet["evolution_stage"] = self._get_evolution_stage(pet["value"])
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, pet)
                    yield event.plain_result(
                        f"💔 进化失败！{name} 身价下降 {loss}，当前 {pet['value']} 金币。\n"
                        f"💰 消耗 {cost} 金币\n"
                        f"⭐ 当前阶段：{pet['evolution_stage']}\n"
                        f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
                    )

    # ==================== 命令：我的宠物 ====================
    @filter.command("我的宠物")
    async def my_pets(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 加入锁机制以检测爆仓
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            # 【新增】检查公寓容量并强制放生
            released, msg = await self._check_and_release_excess_pets(group_id, user_id, event)
            if released:
                if msg: yield msg
                # 如果触发了放生，重新获取数据
                user = self._get_user_data(group_id, user_id)
            else:
                user = self._get_user_data(group_id, user_id)

            # 更新利息并检查强制清算
            self._update_loan_interest(user)
            if await self._check_and_liquidate(event, group_id, user_id, user):
                return

            self._save_user_data(group_id, user_id, user)

            pets = user.get("pets", [])
            capacity = self._get_pet_capacity(user)
            house_count = user.get("house_count", 1)
            
            lines = [f"【🐾 我的宠物】({len(pets)}/{capacity})"]

            if not pets:
                lines.append("你还没有宠物。")
            else:
                for pid in pets:
                    pet = self._get_user_data(group_id, pid)
                    name = pet.get("nickname") or await self._fetch_nickname(event, pid)
                    value = pet.get("value", 100)
                    stage = pet.get("evolution_stage", "普通")
                    lines.append(f"[{stage}] {name} | 💰 身价：{value}")

            coins = user.get("coins", 0)
            bank = user.get("bank", 0)
            bank_level = user.get("bank_level", 1)
            loan = user.get("loan_amount", 0)

            lines.append(f"\n🏠 我的房产：{house_count} 套公寓")
            lines.append(f"💵 当前余额：{coins} 金币")
            lines.append(f"🏦 银行存款：{bank} 金币 (Lv.{bank_level})")
            if loan > 0:
                lines.append(f"💸 银行欠款：{loan} 金币")
                if user.get("loan_interest_frozen", False):
                    lines.append(f"❄️ (利息已冻结)")

            lines.append(f"💎 总资产：{coins + bank - loan} 金币")

            yield event.plain_result("\n".join(lines))

    # ==================== 命令：银行信息 ====================
    @filter.command("银行信息")
    async def bank_info(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            coins_before = user.get("coins", 0)

            self._update_loan_interest(user)
            if await self._check_and_liquidate(event, group_id, user_id, user):
                return

            self._save_user_data(group_id, user_id, user)

            bank = user.get("bank", 0)
            level = user.get("bank_level", 1)
            limit = self._get_bank_limit(level)
            rate = self.config.get("bank_interest_rate", 0.01)
            next_cost = self._get_upgrade_cost(level)

            last_interest = user.get("last_interest_time", int(time.time()))
            now = int(time.time())
            hours = min((now - last_interest) // 3600, self.config.get("bank_max_interest_time", 24))
            potential_interest = self._calculate_compound_interest(bank, rate, hours) if bank > 0 else 0

            loan = user.get("loan_amount", 0)

            message = (
                f"【🏦 银行信息】\n"
                f"💰 当前存款：{bank} 金币\n"
                f"⭐ 信用等级：Lv.{level}\n"
                f"📦 存储上限：{limit} 金币\n"
                f"📈 每小时利息：{rate * 100}%（复利）\n"
                f"💵 可领利息：{potential_interest} 金币\n"
                f"⬆️ 下次升级费用：{next_cost} 金币"
            )

            if loan > 0:
                principal = user.get("loan_principal", 0)
                loan_limit = self._get_loan_limit(level)
                loan_rate = self.config.get("loan_interest_rate", 0.05)
                loan_info = (
                    f"\n----------------------\n"
                    f"【💸 贷款详情】\n"
                    f"当前欠款：{loan} / {loan_limit} 金币\n"
                    f"  (其中本金: {principal})"
                )
                if user.get("loan_interest_frozen", False):
                    loan_info += "\n❄️ 状态：坏账，利息已冻结"
                else:
                    loan_info += f"\n📉 贷款利率：{loan_rate * 100}%/小时"
                message += loan_info

            yield event.plain_result(message)

    # ==================== 命令：升级信用 ====================
    @filter.command("升级信用")
    async def upgrade_bank(self, event: AstrMessageEvent):
        """升级银行信用等级"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            # 【新增】检查是否有未还清的贷款
            current_loan = user.get("loan_amount", 0)
            if current_loan > 0:
                yield event.plain_result(
                    f"❌ 你还有 {current_loan} 金币的未清欠款，必须先还清贷款才能升级信用等级！\n"
                    f"💡 提示：使用 /还款 来偿还贷款。"
                )
                return
            
            level = user.get("bank_level", 1)
            cost = self._get_upgrade_cost(level)

            if user.get("coins", 0) < cost:
                yield event.plain_result(f"❌ 升级需要 {cost} 金币，你的余额不足。")
                return

            coins_before = user.get("coins", 0)
            user["coins"] -= cost
            user["bank_level"] = level + 1
            self._save_user_data(group_id, user_id, user)
            new_limit = self._get_bank_limit(user["bank_level"])

            yield event.plain_result(
                f"✅ 升级成功！信用等级提升至 Lv.{user['bank_level']}\n"
                f"📦 新存储上限：{new_limit} 金币\n"
                f"💰 消耗 {cost} 金币\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    # ==================== 命令：银行利息 ====================
    @filter.command("银行利息")
    async def bank_interest_rate(self, event: AstrMessageEvent):
        """查看当前利息率"""
        rate = self.config.get("bank_interest_rate", 0.01)
        max_hours = self.config.get("bank_max_interest_time", 24)
        yield event.plain_result(
            f"【💹 银行利息说明】\n"
            f"📈 每小时利率：{rate * 100}%\n"
            f"🔄 计息方式：复利\n"
            f"⏰ 最大计息时间：{max_hours} 小时\n\n"
            f"示例：存款1000金币，24小时后：\n"
            f"利息 = 1000 × (1.01)^24 - 1000 ≈ {self._calculate_compound_interest(1000, rate, max_hours)} 金币"
        )

    # ==================== 命令：领取利息 ====================
    @filter.command("领取利息")
    async def collect_interest(self, event: AstrMessageEvent):
        """领取银行利息"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            bank = user.get("bank", 0)

            if bank == 0:
                yield event.plain_result("❌ 你没有银行存款，无法领取利息。")
                return

            last_interest = user.get("last_interest_time", int(time.time()))
            now = int(time.time())
            max_hours = self.config.get("bank_max_interest_time", 24)
            hours = min((now - last_interest) // 3600, max_hours)

            if hours < 1:
                yield event.plain_result("❌ 暂无利息可领取（至少需要1小时）。")
                return

            rate = self.config.get("bank_interest_rate", 0.01)
            interest = self._calculate_compound_interest(bank, rate, hours)

            user["last_interest_time"] = now
            coins_before = user.get("coins", 0)
            user["coins"] = user.get("coins", 0) + interest
            self._save_user_data(group_id, user_id, user)

            yield event.plain_result(
                f"✅ 成功领取利息 {interest} 金币到余额。\n"
                f"⏰ 计息时长：{hours} 小时\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}\n"
                f"🏦 当前存款：{user['bank']} 金币"
            )

    # ==================== 命令：存款 ====================
    @filter.command("存款")
    async def deposit(self, event: AstrMessageEvent, amount: int):
        """存款到银行"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        if amount <= 0:
            yield event.plain_result("❌ 金额必须大于 0。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            coins_before = user.get("coins", 0)
            bank_before = user.get("bank", 0)
            interest = self._settle_bank_interest(user)

            if user.get("coins", 0) < amount:
                yield event.plain_result("❌ 现金不足。")
                return

            # 检查银行容量
            level = user.get("bank_level", 1)
            limit = self._get_bank_limit(level)
            current_bank = user.get("bank", 0)

            if current_bank + amount > limit:
                available = limit - current_bank
                yield event.plain_result(
                    f"❌ 存款失败！当前存款 {current_bank}，上限 {limit}，\n"
                    f"最多还能存 {available} 金币。\n"
                    f"提示：可使用 /升级信用 提升存储上限。"
                )
                return

            user["coins"] -= amount
            user["bank"] = current_bank + amount
            self._save_user_data(group_id, user_id, user)

            interest_msg = f"💹 已结算利息 {interest} 金币\n" if interest > 0 else ""
            yield event.plain_result(
                f"✅ 存款成功！存入 {amount} 金币。\n"
                f"{interest_msg}"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}\n"
                f"{self._format_amount_change(bank_before, user['bank'], '🏦 存款')}\n"
                f"📦 存储上限：{limit} 金币"
            )

    # ==================== 命令：取款 ====================
    @filter.command("取款")
    async def withdraw(self, event: AstrMessageEvent, amount: int):
        """从银行取款"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        if amount <= 0:
            yield event.plain_result("❌ 金额必须大于 0。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            coins_before = user.get("coins", 0)
            bank_before = user.get("bank", 0)
            interest = self._settle_bank_interest(user)

            if user.get("bank", 0) < amount:
                yield event.plain_result("❌ 银行存款不足。")
                return

            user["bank"] -= amount
            user["coins"] = user.get("coins", 0) + amount
            self._save_user_data(group_id, user_id, user)

            interest_msg = f"💹 已结算利息 {interest} 金币\n" if interest > 0 else ""
            yield event.plain_result(
                f"✅ 取款成功！取出 {amount} 金币。\n"
                f"{interest_msg}"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}\n"
                f"{self._format_amount_change(bank_before, user['bank'], '🏦 存款')}"
            )

    # ==================== 命令：贷款 ====================
    @filter.command("贷款")
    async def take_loan(self, event: AstrMessageEvent):  # 【修改】移除 amount: int 参数
        """向银行贷款"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 【新增】手动提取金额并进行校验
        amount = self._extract_amount(event)
        if not amount or amount <= 0:
            yield event.plain_result("❌ 请指定有效的贷款金额。用法: /贷款 500")
            return

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 你在监狱中，银行拒绝了你的贷款申请。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            self._update_loan_interest(user)

            if await self._check_and_liquidate(event, group_id, user_id, user):
                return

            level = user.get("bank_level", 1)
            limit = self._get_loan_limit(level)
            current_loan = user.get("loan_amount", 0)

            if current_loan + amount > limit:
                can_borrow = max(0, limit - current_loan)
                yield event.plain_result(f"❌ 信用额度不足！上限 {limit}，剩余可贷 {can_borrow}。")
                self._save_user_data(group_id, user_id, user)
                return

            coins_before = user.get("coins", 0)
            user["loan_amount"] = current_loan + amount
            user["coins"] = user.get("coins", 0) + amount
            user["loan_principal"] = user.get("loan_principal", 0) + amount

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 贷款成功！获得 {amount} 金币。\n"
            msg += f"💸 当前欠款：{user['loan_amount']} (本金 {user['loan_principal']})\n"
            msg += f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}\n"
            msg += "⚠️ 请按时还款，利息按小时复利计算！"

            yield event.plain_result(msg)

    # ==================== 命令：还款 ====================
    @filter.command("还款")
    async def repay_loan(self, event: AstrMessageEvent, amount: Optional[int] = None):
        """偿还银行贷款 (不填金额默认还清所有)"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法办理银行业务。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            # 1. 结算利息
            self._update_loan_interest(user)

            # 2. 【新增】检查强制清算
            if await self._check_and_liquidate(event, group_id, user_id, user):
                return

            current_loan = user.get("loan_amount", 0)
            principal = user.get("loan_principal", 0)

            if current_loan <= 0:
                yield event.plain_result("✅ 你当前没有欠款，无债一身轻！")
                user["loan_principal"] = 0
                user["loan_interest_frozen"] = False
                self._save_user_data(group_id, user_id, user)
                return

            user_coins = user.get("coins", 0)
            target_amount = amount if amount is not None else current_loan
            if target_amount <= 0:
                yield event.plain_result("❌ 还款金额必须大于 0。")
                return

            real_repay = min(target_amount, current_loan)

            if user_coins < real_repay:
                yield event.plain_result(f"❌ 余额不足！需还 {real_repay}，余额 {user_coins}。")
                self._save_user_data(group_id, user_id, user)
                return

            # 执行还款
            coins_before = user.get("coins", 0)
            user["coins"] -= real_repay
            user["loan_amount"] -= real_repay

            # 更新本金
            # 逻辑：只要当前的欠款少于记录的本金，说明利息已经还完了，开始还本金了
            if user["loan_amount"] < principal:
                user["loan_principal"] = user["loan_amount"]

            # 如果还清了
            if user["loan_amount"] <= 0:
                user["loan_amount"] = 0
                user["loan_principal"] = 0
                user["loan_interest_frozen"] = False  # 解除冻结
                user["loan_transfers"] = []  # 贷款还清，以前的转账记录既往不咎

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 还款成功！支付 {real_repay} 金币。\n"
            msg += f"💸 剩余欠款：{user['loan_amount']} (本金 {user['loan_principal']})\n"
            msg += f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"

            yield event.plain_result(msg)

    # ==================== 命令：转账 ====================
    @filter.command("转账")
    async def transfer(self, event: AstrMessageEvent):
        """转账给其他玩家"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)
        amount = self._extract_amount(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定转账目标。")
            return

        if not amount or amount <= 0:
            yield event.plain_result("❌ 请指定有效的转账金额。用法: /转账 @用户 金额")
            return

        if target_id == user_id:
            yield event.plain_result("❌ 不能转账给自己。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        # 检查目标是否在监狱
        target_jailed, _ = self._check_jailed(group_id, target_id)
        if target_jailed:
            yield event.plain_result("❌ 目标玩家在监狱中，无法转账。")
            return

        # 检查最低转账金额
        min_amount = self.config.get("transfer_min_amount", 100)
        if amount < min_amount:
            yield event.plain_result(f"❌ 最低转账金额为 {min_amount} 金币。")
            return

        # 使用交易锁
        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                target_data = self._get_user_data(group_id, target_id)

                # 检查冷却（使用配置）
                cooldown_seconds = self.config.get("transfer_cooldown", 1800)
                in_cooldown, remain = self._check_cooldown(user_data, "transfer", cooldown_seconds)
                if in_cooldown:
                    mins = remain // 60
                    secs = remain % 60
                    yield event.plain_result(f"⏰ 转账冷却中，剩余 {mins}分{secs}秒。")
                    return

                # 计算手续费
                fee_rate = self.config.get("transfer_fee_rate", 0.1)
                fee = int(amount * fee_rate)
                total_cost = amount + fee

                if user_data.get("coins", 0) < total_cost:
                    yield event.plain_result(
                        f"❌ 金币不足。\n"
                        f"转账金额：{amount}\n"
                        f"手续费：{fee} ({int(fee_rate * 100)}%)\n"
                        f"总计需要：{total_cost} 金币"
                    )
                    return

                # 执行转账
                sender_before = user_data.get("coins", 0)
                target_before = target_data.get("coins", 0)
                user_data["coins"] -= total_cost
                target_data["coins"] = target_data.get("coins", 0) + amount
                self._set_cooldown(user_data, "transfer")

                # 记录转账历史
                timestamp = int(time.time())
                user_transfer = {
                    "type": "send",
                    "target": target_id,
                    "amount": amount,
                    "fee": fee,
                    "timestamp": timestamp
                }
                target_transfer = {
                    "type": "receive",
                    "target": user_id,
                    "amount": amount,
                    "fee": 0,
                    "timestamp": timestamp
                }

                user_data.setdefault("transfer_history", []).insert(0, user_transfer)
                target_data.setdefault("transfer_history", []).insert(0, target_transfer)

                # 保留最近20条记录
                user_data["transfer_history"] = user_data["transfer_history"][:20]
                target_data["transfer_history"] = target_data["transfer_history"][:20]

                # 记录带病转账（仅在转账成功后）
                loan_status_msg = ""
                if user_data.get("loan_amount", 0) > 0:
                    loan_status_msg = "\n⚠️ 注意：您当前处于负债状态！此笔转账已被银行记录。若您逾期未还款，银行有权追回此笔资金！"
                    transfer_record = {
                        "target": target_id,
                        "amount": amount,
                        "time": int(time.time()),
                        "target_name": target_data.get("nickname", target_id)
                    }
                    user_data.setdefault("loan_transfers", []).append(transfer_record)

                self._save_user_data(group_id, user_id, user_data)
                self._save_user_data(group_id, target_id, target_data)

                user_name = user_data.get("nickname") or await self._fetch_nickname(event, user_id)

                target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)

                yield event.plain_result(
                    f"✅ 转账成功！\n"
                    f"💸 从 {user_name} 转给 {target_name}\n"
                    f"💰 转账金额：{amount} 金币\n"
                    f"💵 手续费：{fee} 金币 ({int(fee_rate * 100)}%)\n"
                    f"{self._format_amount_change(sender_before, user_data['coins'], '📊 你的余额')}\n"
                    f"{self._format_amount_change(target_before, target_data['coins'], '📊 对方余额')}"
                    f"{loan_status_msg}"
                )

    # ==================== 命令：转账记录 ====================
    @filter.command("转账记录")
    async def transfer_history(self, event: AstrMessageEvent):
        """查看转账记录"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user = self._get_user_data(group_id, user_id)

        history = user.get("transfer_history", [])
        if not history:
            yield event.plain_result("❌ 暂无转账记录。")
            return

        lines = ["【💸 转账记录】（最近10条）"]
        for i, record in enumerate(history[:10], 1):
            record_type = record.get("type")
            target_id = record.get("target")
            amount = record.get("amount", 0)
            fee = record.get("fee", 0)
            timestamp = record.get("timestamp", 0)

            # 格式化时间
            dt = datetime.fromtimestamp(timestamp)
            time_str = dt.strftime("%m-%d %H:%M")

            target_name = await self._fetch_nickname(event, target_id)

            if record_type == "send":
                lines.append(f"{i}. [{time_str}] 转出 {amount} 给 {target_name}（手续费{fee}）")
            else:
                lines.append(f"{i}. [{time_str}] 收到 {amount} 来自 {target_name}")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：宠物身价排行榜 ====================
    @filter.command("宠物身价排行榜")
    async def value_ranking(self, event: AstrMessageEvent, page: int = 1):
        """查看宠物身价排行榜（支持分页）"""
        group_id = str(event.message_obj.group_id)
        pets = self._get_pets_in_group(group_id)

        if not pets:
            yield event.plain_result("本群暂无宠物数据。")
            return

        ranked = sorted(pets.items(), key=lambda x: x[1].get("value", 100), reverse=True)

        # 分页逻辑
        page_size = 10
        total = len(ranked)
        total_pages = (total + page_size - 1) // page_size
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size

        lines = [f"【💎 宠物身价排行榜】第 {page}/{total_pages} 页"]

        for i, (uid, data) in enumerate(ranked[start:end], start + 1):
            name = data.get("nickname") or await self._fetch_nickname(event, uid)
            value = data.get("value", 100)
            stage = data.get("evolution_stage", "普通")
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} [{stage}] {name} - {value} 金币")

        if total_pages > 1:
            lines.append(f"\n💡 发送 /宠物身价排行榜 {page + 1 if page < total_pages else 1} 查看其他页")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：宠物资金排行榜 ====================
    @filter.command("宠物资金排行榜")
    async def coin_ranking(self, event: AstrMessageEvent, page: int = 1):
        """查看宠物资金排行榜（支持分页）"""
        group_id = str(event.message_obj.group_id)
        pets = self._get_pets_in_group(group_id)

        if not pets:
            yield event.plain_result("本群暂无宠物数据。")
            return

        ranked = sorted(pets.items(), key=lambda x: x[1].get("coins", 0), reverse=True)

        # 分页逻辑
        page_size = 10
        total = len(ranked)
        total_pages = (total + page_size - 1) // page_size
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size

        lines = [f"【💰 宠物资金排行榜】第 {page}/{total_pages} 页"]

        for i, (uid, data) in enumerate(ranked[start:end], start + 1):
            name = data.get("nickname") or await self._fetch_nickname(event, uid)
            coins = data.get("coins", 0)
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {name} - {coins} 金币")

        if total_pages > 1:
            lines.append(f"\n💡 发送 /宠物资金排行榜 {page + 1 if page < total_pages else 1} 查看其他页")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：群内十大首富 ====================
    @filter.command("群内十大首富")
    async def total_rich_ranking(self, event: AstrMessageEvent, page: int = 1):
        """查看总资产排行榜（支持分页）"""
        group_id = str(event.message_obj.group_id)
        pets = self._get_pets_in_group(group_id)

        if not pets:
            yield event.plain_result("本群暂无宠物数据。")
            return

        ranked = sorted(
            pets.items(),
            key=lambda x: x[1].get("coins", 0) + x[1].get("bank", 0),
            reverse=True
        )

        # 分页逻辑
        page_size = 10
        total = len(ranked)
        total_pages = (total + page_size - 1) // page_size
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size

        lines = [f"【👑 群内十大首富】第 {page}/{total_pages} 页"]

        for i, (uid, data) in enumerate(ranked[start:end], start + 1):
            name = data.get("nickname") or await self._fetch_nickname(event, uid)
            coins = data.get("coins", 0)
            bank = data.get("bank", 0)
            total_assets = coins + bank
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {name} - {total_assets} 金币（余额{coins}+存款{bank}）")

        if total_pages > 1:
            lines.append(f"\n💡 发送 /群内十大首富 {page + 1 if page < total_pages else 1} 查看其他页")

        yield event.plain_result("\n".join(lines))

    # ==================== 命令：PK ====================
    @filter.command("PK", alias={"pk", "决斗"})
    async def pk_battle(self, event: AstrMessageEvent):
        """宠物决斗"""
        # 私聊检测
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此功能仅限群聊使用。")
            return

        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定要挑战的对手。")
            return

        if target_id == user_id:
            yield event.plain_result("❌ 不能和自己决斗。")
            return

        # 检查监狱状态
        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        # 使用交易锁
        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                target_data = self._get_user_data(group_id, target_id)

                # 检查双方是否都有宠物
                user_pets = user_data.get("pets", [])
                target_pets = target_data.get("pets", [])

                if not user_pets:
                    yield event.plain_result("❌ 你还没有宠物，无法参与决斗。")
                    return

                if not target_pets:
                    target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
                    yield event.plain_result(f"❌ {target_name} 还没有宠物，无法挑战。")
                    return

                # 检查冷却（使用配置）
                cooldown_seconds = self.config.get("pk_cooldown", 3600)
                in_cooldown, remain = self._check_cooldown(user_data, "pk", cooldown_seconds)
                if in_cooldown:
                    mins = remain // 60
                    yield event.plain_result(f"⏰ PK 冷却中，剩余 {mins} 分钟。")
                    return

                # 获取双方最强宠物（身价最高的）
                user_pet_id = max(user_pets, key=lambda pid: self._get_user_data(group_id, pid).get("value", 100))
                target_pet_id = max(target_pets, key=lambda pid: self._get_user_data(group_id, pid).get("value", 100))

                user_pet = self._get_user_data(group_id, user_pet_id)
                target_pet = self._get_user_data(group_id, target_pet_id)

                user_pet_name = user_pet.get("nickname") or await self._fetch_nickname(event, user_pet_id)
                target_pet_name = target_pet.get("nickname") or await self._fetch_nickname(event, target_pet_id)

                user_pet_value = user_pet.get("value", 100)
                target_pet_value = target_pet.get("value", 100)
                user_pet_stage = user_pet.get("evolution_stage", "普通")
                target_pet_stage = target_pet.get("evolution_stage", "普通")

                # 战斗力计算（身价 × 随机系数 0.8~1.2）
                user_power = user_pet_value * random.uniform(0.8, 1.2)
                target_power = target_pet_value * random.uniform(0.8, 1.2)

                # 设置冷却（双方都进冷却）
                self._set_cooldown(user_data, "pk")
                self._set_cooldown(target_data, "pk")

                # 判定胜负
                if user_power > target_power:
                    # 用户胜利
                    prize = int(target_pet_value * 0.1)
                    user_pet["value"] += prize
                    target_pet["value"] = max(100, target_pet["value"] - prize)

                    # 更新进化阶段
                    user_pet["evolution_stage"] = self._get_evolution_stage(user_pet["value"])
                    target_pet["evolution_stage"] = self._get_evolution_stage(target_pet["value"])

                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)
                    self._save_user_data(group_id, user_pet_id, user_pet)
                    self._save_user_data(group_id, target_pet_id, target_pet)

                    yield event.plain_result(
                        f"⚔️ 【PK 决斗】\n"
                        f"你的 [{user_pet_stage}]{user_pet_name}（{user_pet_value}）发起挑战！\n"
                        f"对方 [{target_pet_stage}]{target_pet_name}（{target_pet_value}）迎战！\n\n"
                        f"⚡ 战斗过程：{user_pet_name} 爆发出 {int(user_power)} 点战力，压制了对手！\n\n"
                        f"🏆 **你赢了！**\n"
                        f"📈 你的宠物身价 +{prize}（当前 {user_pet['value']}）\n"
                        f"📉 对方宠物身价 -{prize}（当前 {target_pet['value']}）"
                    )
                else:
                    # 用户失败
                    loss = int(user_pet_value * 0.1)
                    target_pet["value"] += loss
                    user_pet["value"] = max(100, user_pet["value"] - loss)

                    # 更新进化阶段
                    user_pet["evolution_stage"] = self._get_evolution_stage(user_pet["value"])
                    target_pet["evolution_stage"] = self._get_evolution_stage(target_pet["value"])

                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)
                    self._save_user_data(group_id, user_pet_id, user_pet)
                    self._save_user_data(group_id, target_pet_id, target_pet)

                    yield event.plain_result(
                        f"⚔️ 【PK 决斗】\n"
                        f"你的 [{user_pet_stage}]{user_pet_name}（{user_pet_value}）发起挑战！\n"
                        f"对方 [{target_pet_stage}]{target_pet_name}（{target_pet_value}）迎战！\n\n"
                        f"⚡ 战斗过程：{target_pet_name} 爆发出 {int(target_power)} 点战力，完胜！\n\n"
                        f"💔 **你输了...**\n"
                        f"📉 你的宠物身价 -{loss}（当前 {user_pet['value']}）\n"
                        f"📈 对方宠物身价 +{loss}（当前 {target_pet['value']}）"
                    )

    # ==================== 命令：抢劫 ====================
    @filter.command("抢劫")
    async def rob(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定抢劫目标。")
            return

        if target_id == user_id:
            yield event.plain_result("❌ 不能抢劫自己。")
            return

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            hours = remain // 3600
            mins = (remain % 3600) // 60
            yield event.plain_result(f"🔒 你还在监狱中，剩余 {hours}小时{mins}分钟。")
            return

        lock_ids = sorted([user_id, target_id])
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[0]}"):
            async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{lock_ids[1]}"):
                user_data = self._get_user_data(group_id, user_id)
                target_data = self._get_user_data(group_id, target_id)

                # 检查冷却
                cooldown_seconds = self.config.get("rob_cooldown", 3600)
                in_cooldown, remain = self._check_cooldown(user_data, "rob", cooldown_seconds)
                if in_cooldown:
                    mins = remain // 60
                    yield event.plain_result(f"⏰ 抢劫冷却中，剩余 {mins} 分钟。")
                    return

                # 【新增】检查护身符
                target_inventory = target_data.get("inventory", {})
                if target_inventory.get("102", 0) > 0:
                    target_inventory["102"] -= 1
                    if target_inventory["102"] <= 0:
                        del target_inventory["102"]
                    self._save_user_data(group_id, target_id, target_data)
                    self._set_cooldown(user_data, "rob") # 仍然产生冷却
                    
                    target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
                    yield event.plain_result(f"🛡️ 糟糕！{target_name} 佩戴了护身符，你的行动被抵挡了！")
                    return

                # ==================== 新增：待处理案件超时逻辑 ====================
                pending_penalty = user_data.get("rob_pending_penalty")
                if pending_penalty:
                    TIMEOUT_SECONDS = 3600  # 设置超时时间为 1 小时

                    penalty_time = pending_penalty.get("time", 0)
                    if int(time.time()) - penalty_time > TIMEOUT_SECONDS:
                        # 案件已超时，强制坐牢
                        jail_hours = self.config.get("rob_jail_hours", 24)
                        user_data["jailed_until"] = int(time.time()) + (jail_hours * 3600)
                        user_data["rob_pending_penalty"] = None  # 清除状态
                        user_data["rob_fail_streak"] = 0  # 坐牢后重置连败
                        self._save_user_data(group_id, user_id, user_data)
                        yield event.plain_result(
                            f"⏰ 你因超过1小时未处理抢劫案件，已被系统强制送入监狱 {jail_hours} 小时！")
                        return  # 终止后续操作
                    else:
                        # 案件未超时，提醒玩家
                        yield event.plain_result("🔒 你还有未处理的抢劫案件！请先选择 /交罚款 或 /坐牢。")
                        return
                # ==================== 修改结束 ====================

                if target_data.get("coins", 0) == 0:
                    yield event.plain_result("❌ 目标余额为0，无法抢劫。")
                    return

                self._set_cooldown(user_data, "rob")

                attacker_level = user_data.get("bank_level", 1)
                target_level = target_data.get("bank_level", 1)
                success_rate = self._calculate_rob_success_rate(attacker_level, target_level)

                user_name = user_data.get("nickname") or await self._fetch_nickname(event, user_id)
                target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)

                if random.random() < success_rate:
                    # 抢劫成功
                    user_before = user_data.get("coins", 0)
                    target_before = target_data.get("coins", 0)
                    rate = random.randint(5, 20) / 100
                    amount = int(target_data["coins"] * rate)
                    target_data["coins"] -= amount
                    user_data["coins"] = user_data.get("coins", 0) + amount

                    # 成功后重置连败
                    user_data["rob_fail_streak"] = 0

                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)

                    yield event.plain_result(
                        f"💰 抢劫成功！{user_name} 从 {target_name} 手中抢走 {amount} 金币。\n"
                        f"🎲 成功率：{int(success_rate * 100)}%\n"
                        f"{self._format_amount_change(user_before, user_data['coins'], '💵 你的余额')}\n"
                        f"{self._format_amount_change(target_before, target_data['coins'], '💵 对方余额')}"
                    )
                else:
                    # 抢劫失败：计算罚款并暂存状态
                    user_value = user_data.get("value", 100)  # 身价
                    streak = user_data.get("rob_fail_streak", 0)
                    multiplier = 1.5 + (streak * 0.5)
                    fine = int(user_value * multiplier)

                    # 记录待处理状态
                    user_data["rob_pending_penalty"] = {
                        "amount": fine,
                        "time": int(time.time())
                    }
                    self._save_user_data(group_id, user_id, user_data)
                    jail_hours = self.config.get("rob_jail_hours", 24)

                    yield event.plain_result(
                        f"🚨 抢劫失败！{user_name} 被当场抓获！\n"
                        f"⚖️ 当前连败次数：{streak} (罚款倍率 {multiplier}x)\n"
                        f"💸 罚款金额：{fine} 金币 (按身价计算)\n"
                        f"⚠️ 请在以下选项中二选一：\n"
                        f"1. 发送 /交罚款 (扣除金币，保留自由)\n"
                        f"2. 发送 /坐牢 (无需罚款，监禁{jail_hours}小时)"
                    )

    # ==================== 命令：交罚款 ====================
    @filter.command("交罚款")
    async def pay_rob_fine(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            pending = user_data.get("rob_pending_penalty")

            if not pending:
                yield event.plain_result("❓ 你当前没有待处理的抢劫案件。")
                return

            fine = pending["amount"]
            if user_data.get("coins", 0) < fine:
                yield event.plain_result(f"❌ 余额不足！需要 {fine} 金币。请充值或选择 /坐牢。")
                return

            coins_before = user_data.get("coins", 0)
            user_data["coins"] -= fine
            user_data["rob_pending_penalty"] = None  # 清除状态
            user_data["rob_fail_streak"] += 1  # 增加连败次数，下次更贵

            self._save_user_data(group_id, user_id, user_data)
            yield event.plain_result(
                f"💸 罚款缴纳成功！扣除 {fine} 金币。下次抢劫失败罚款倍率将提升。\n"
                f"{self._format_amount_change(coins_before, user_data['coins'], '💵 余额')}"
            )

    # ==================== 命令：坐牢 ====================
    @filter.command("坐牢")
    async def go_to_jail(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user_data = self._get_user_data(group_id, user_id)
            if not user_data.get("rob_pending_penalty"):
                yield event.plain_result("❓ 你当前没有待处理的抢劫案件。")
                return

            jail_hours = self.config.get("rob_jail_hours", 24)
            user_data["jailed_until"] = int(time.time()) + (jail_hours * 3600)
            user_data["rob_pending_penalty"] = None  # 清除状态
            user_data["rob_fail_streak"] = 0  # 坐牢后重置连败计数

            self._save_user_data(group_id, user_id, user_data)
            yield event.plain_result(f"⛓️ 你选择了坐牢。将在监狱中度过 {jail_hours} 小时。")

    # ==================== 管理员命令 ====================
    def _init_admins(self) -> List[str]:
        """
        【新增】初始化管理员列表
        从配置中获取管理员ID，支持多种配置方式
        """
        admins = set()

        def parse_admins(value):
            """辅助解析函数"""
            result = set()
            if isinstance(value, list):
                for item in value:
                    result.update(parse_admins(item))
            elif isinstance(value, str):
                # 支持逗号、分号、空格分隔
                import re
                parts = re.split(r'[,;，；\s]+', value)
                for part in parts:
                    s = part.strip()
                    if s.isdigit():
                        result.add(s)
            elif isinstance(value, (int, float)):
                result.add(str(int(value)))
            return result

        # 方式1：从 config 中的 admin_uins 字段获取
        admin_conf = self.config.get("admin_uins", [])
        admins.update(parse_admins(admin_conf))
        
        # 方式2：尝试获取 admins_id (兼容其他配置方式)
        try:
            # 尝试直接从 config 获取
            if "admins_id" in self.config:
                admins.update(parse_admins(self.config["admins_id"]))
            
            # 保留原有的 context.get_config 逻辑
            global_config = self.context.get_config()
            if global_config and isinstance(global_config, dict):
                if "admins_id" in global_config:
                    admins.update(parse_admins(global_config["admins_id"]))
        except Exception as e:
            logger.warning(f"[宠物市场] 从全局配置获取管理员失败: {e}")
        
        final_list = list(admins)
        
        # 如果没有配置任何管理员，禁用管理员指令并提示配置
        if not final_list:
            logger.warning("[宠物市场] 未配置管理员列表，管理员指令将不可用。请在 WebUI 配置 admin_uins。")
        else:
            logger.info(f"[宠物市场] 已加载 {len(final_list)} 个管理员: {final_list}")
        
        return final_list

    def _is_admin(self, user_id: str) -> bool:
        """检查是否是管理员"""
        user_id = str(user_id).strip()
        # 使用初始化时加载的管理员列表
        return user_id in self.admins

    @filter.command("我发钱")
    async def give_me_money(self, event: AstrMessageEvent, amount: int):
        """管理员给自己发钱"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        if amount <= 0 or amount > 10000:
            yield event.plain_result("❌ 一次最多 10000 金币。")
            return

        group_id = str(event.message_obj.group_id)
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            coins_before = user.get("coins", 0)
            user["coins"] = user.get("coins", 0) + amount
            self._save_user_data(group_id, user_id, user)
            yield event.plain_result(
                f"✅ 已发放 {amount} 金币。\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    @filter.command("跳过冷却")
    async def skip_cooldown(self, event: AstrMessageEvent):
        """管理员清空自己的冷却时间"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        group_id = str(event.message_obj.group_id)
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            user["cooldowns"] = {}
            self._save_user_data(group_id, user_id, user)
            yield event.plain_result("✅ 已清空所有冷却时间。")

    # ==================== 命令：购买公寓 ====================
    @filter.command("购买公寓")
    async def buy_house(self, event: AstrMessageEvent):
        """购买公寓扩充宠物上限"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法进行房产交易。")
            return

        price = self.config.get("house_price", 20000)
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            if user.get("coins", 0) < price:
                yield event.plain_result(f"❌ 金币不足！购买一间公寓需要 {price} 金币。")
                return

            coins_before = user.get("coins", 0)
            user["coins"] -= price
            user["house_count"] = user.get("house_count", 1) + 1
            
            new_capacity = self._get_pet_capacity(user)
            
            self._save_user_data(group_id, user_id, user)
            
            yield event.plain_result(
                f"🎉 购房成功！恭喜你成为新的房产主！\n"
                f"🏠 当前房产：{user['house_count']} 套\n"
                f"🐾 容纳上限：{new_capacity} 只宠物\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    # ==================== 命令：租房 ====================
    @filter.command("租房")
    async def rent_house(self, event: AstrMessageEvent):
        """租借临时公寓（7天，增加1间容量）"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法租房。")
            return

        price = self.config.get("house_rent_price", 2000)
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            if user.get("coins", 0) < price:
                yield event.plain_result(f"❌ 金币不足！租借公寓(7天)需要 {price} 金币。")
                return

            current_expiry = user.get("rented_house_expiry", 0)
            now = int(time.time())
            
            # 如果已经在租，续费7天，否则从现在开始7天
            if current_expiry > now:
                new_expiry = current_expiry + (7 * 86400)
                msg_type = "续租"
            else:
                new_expiry = now + (7 * 86400)
                msg_type = "租房"
                
            coins_before = user.get("coins", 0)
            user["coins"] -= price
            user["rented_house_expiry"] = new_expiry
            
            new_capacity = self._get_pet_capacity(user)
            days_left = (new_expiry - now) // 86400
            
            self._save_user_data(group_id, user_id, user)
            
            yield event.plain_result(
                f"🎉 {msg_type}成功！\n"
                f"📅 到期时间：{days_left}天后\n"
                f"🐾 临时扩容：+5 容量 (总上限: {new_capacity})\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    # ==================== 命令：我的公寓 ====================
    @filter.command("我的公寓", alias={"我的房产"})
    async def my_house(self, event: AstrMessageEvent):
        """查看公寓及入住宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        # 解析参数看看是不是查特定公寓
        args = event.message_str.split()
        house_idx = None
        if len(args) > 1 and args[1].replace('公寓', '').isdigit():
            house_idx = int(args[1].replace('公寓', ''))

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            # 检查强制放生
            released, msg = await self._check_and_release_excess_pets(group_id, user_id, event)
            if released:
                if msg: yield msg
                user = self._get_user_data(group_id, user_id) # reload
            else:
                user = self._get_user_data(group_id, user_id)

            house_count = user.get("house_count", 1)
            pets = user.get("pets", [])
            per_house = self.config.get("pet_per_house", 5)
            
            rented_expiry = user.get("rented_house_expiry", 0)
            has_rented = rented_expiry > int(time.time())
            
            total_houses = house_count + (1 if has_rented else 0)
            capacity = total_houses * per_house

            if house_idx is not None:
                # 查看特定公寓
                if house_idx < 1 or house_idx > total_houses:
                    yield event.plain_result(f"❌ 你只有 {total_houses} 间公寓。")
                    return
                
                start_idx = (house_idx - 1) * per_house
                end_idx = start_idx + per_house
                house_pets = pets[start_idx:end_idx]
                
                house_name = f"公寓#{house_idx}"
                if has_rented and house_idx == total_houses:
                    house_name += " (租赁)"
                
                lines = [f"🏠 【{house_name}】入住名单"]
                if not house_pets:
                    lines.append("  (空置中...)")
                else:
                    for pid in house_pets:
                        p_data = self._get_user_data(group_id, pid)
                        name = p_data.get("nickname") or await self._fetch_nickname(event, pid)
                        lines.append(f"  🐶 {name} (身价: {p_data.get('value', 100)})")
                
                lines.append(f"\n入住率: {len(house_pets)}/{per_house}")
                yield event.plain_result("\n".join(lines))
                return

            # 如果没有指定公寓，显示概览
            lines = ["🏘️ 【我的不动产中心】"]
            lines.append(f"我的公寓：{house_count} 套 (永久)")
            if has_rented:
                days = (rented_expiry - int(time.time())) // 86400
                lines.append(f"租赁公寓：1 套 (剩余 {days} 天)")
            
            lines.append(f"总计容量：{capacity} 只 (当前: {len(pets)})")
            lines.append("-" * 20)
            
            # 显示每个公寓的简略信息
            for i in range(1, total_houses + 1):
                start = (i - 1) * per_house
                count = 0
                if start < len(pets):
                    count = min(len(pets) - start, per_house)
                
                status = "租赁" if (has_rented and i == total_houses) else "自有"
                bar = "█" * count + "░" * (per_house - count)
                lines.append(f"公寓 #{i} [{status}]: {bar} {count}/{per_house}")
                
            lines.append("\n💡 指令：/公寓 [编号] 查看详情")
            lines.append("💡 指令：/购买公寓 (20000金币) | /租房 (2000金币/7天)")
            
            yield event.plain_result("\n".join(lines))

    # ==================== 命令：商店系统 ====================
    @filter.command("商店", alias={"道具商店"})
    async def shop_view(self, event: AstrMessageEvent):
        """查看道具商店"""
        lines = ["🛒 【宠物百货商店】"]
        lines.append("消耗金币购买道具，增强你的游戏体验！")
        lines.append("-" * 20)
        
        for pid, item in SHOP_ITEMS.items():
            lines.append(f"{item['icon']} [{pid}] {item['name']}")
            lines.append(f"   💰 {item['price']} 金币")
            lines.append(f"   📝 {item['desc']}")
            lines.append("")
            
        lines.append("💡 指令：/购买道具 [ID] (例如: /购买道具 101)")
        lines.append("💡 指令：/我的背包 查看已拥有道具")
        
        yield event.plain_result("\n".join(lines))

    @filter.command("购买道具")
    async def buy_item(self, event: AstrMessageEvent):
        """购买商店道具，支持批量：/购买道具 [ID] *数量"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /购买道具 [道具ID] [*数量]")
            return
            
        # 解析 ID 和 数量
        item_id = None
        count = 1
        
        # 简单解析逻辑：尝试从参数中分离数量
        raw_args = args[1:]
        # 寻找像 *10 这样的参数
        target_count_idx = -1
        for idx, arg in enumerate(raw_args):
            if arg.startswith('*') and arg[1:].isdigit():
                count = int(arg[1:])
                target_count_idx = idx
                break
            elif arg.isdigit() and idx > 0: # 如果是纯数字且不是第一个参数，也可能是数量
                count = int(arg)
                target_count_idx = idx
                break
        
        if target_count_idx != -1:
            # 移除了数量参数，剩下的是 ID
            item_id_list = raw_args[:target_count_idx] + raw_args[target_count_idx+1:]
            if item_id_list: item_id = item_id_list[0]
        else:
            item_id = raw_args[0]
        
        if not item_id or item_id not in SHOP_ITEMS:
            yield event.plain_result("❌ 商品不存在，请检查ID。")
            return
            
        if count <= 0:
            yield event.plain_result("❌ 购买数量必须大于0。")
            return
        
        if count > 100:
            yield event.plain_result("❌ 单次购买上限 100 个。")
            return
            
        item = SHOP_ITEMS[item_id]
        price = item["price"]
        total_price = price * count
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            if user.get("coins", 0) < total_price:
                yield event.plain_result(f"❌ 余额不足！购买 {count} 个 {item['name']} 需要 {total_price} 金币。")
                return

            coins_before = user.get("coins", 0)
            user["coins"] -= total_price
            
            # 由于没有复杂的背包系统，简单用字典计数
            inventory = user.setdefault("inventory", {})
            inventory[item_id] = inventory.get(item_id, 0) + count
            
            self._save_user_data(group_id, user_id, user)
            
            yield event.plain_result(
                f"🎉 购买成功！\n"
                f"获得：{item['icon']} {item['name']} x{count}\n"
                f"花费：{total_price} 金币\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    @filter.command("我的背包", alias={"背包"})
    async def my_inventory(self, event: AstrMessageEvent):
        """查看拥有道具"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            inventory = user.get("inventory", {})
            coins_before = user.get("coins", 0)
            
            if not inventory:
                yield event.plain_result("🎒 你的背包空空如也。")
                return
                
            lines = ["🎒 【我的背包】"]
            for pid, count in inventory.items():
                if count <= 0: continue
                item = SHOP_ITEMS.get(pid, {"name": "未知物品", "icon": "❓"})
                lines.append(f"{item['icon']} {item['name']} x{count} (ID: {pid})")
                
            lines.append("-" * 20)
            lines.append("💡 指令：/使用道具 [ID]")
            
            yield event.plain_result("\n".join(lines))

    @filter.command("使用道具")
    async def use_item(self, event: AstrMessageEvent):
        """使用背包中的道具，支持批量：/使用道具 [ID] *数量"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /使用道具 [道具ID] [*数量]")
            return
            
        # 解析 ID 和 数量 (复用逻辑)
        item_id = None
        count = 1
        raw_args = args[1:]
        target_count_idx = -1
        for idx, arg in enumerate(raw_args):
            if arg.startswith('*') and arg[1:].isdigit():
                count = int(arg[1:])
                target_count_idx = idx
                break
            elif arg.isdigit() and idx > 0:
                count = int(arg)
                target_count_idx = idx
                break
        
        if target_count_idx != -1:
            item_id_list = raw_args[:target_count_idx] + raw_args[target_count_idx+1:]
            if item_id_list: item_id = item_id_list[0]
        else:
            item_id = raw_args[0]
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            inventory = user.get("inventory", {})
            coins_before = user.get("coins", 0)
            
            if inventory.get(item_id, 0) < count:
                yield event.plain_result(f"❌ 数量不足！你只有 {inventory.get(item_id, 0)} 个。")
                return
                
            item = SHOP_ITEMS.get(item_id)
            if not item:
                yield event.plain_result("❌ 道具数据错误。")
                return
                
            # 执行道具效果
            msg = ""
            consumed = True
            
            # ========== 刮刮乐逻辑 (支持批量) ==========
            if item.get("type") == "scratch_card":
                total_win = 0
                win_details = {} # 改为记录获得各个奖项的次数
                
                awards = item.get("awards", [])
                
                # 预计算概率区间，提高效率
                # awards: [{"prob": 0.4, ...}, ...]
                
                for _ in range(count):
                    r = random.random()
                    cumulative = 0.0
                    prize = 0
                    prize_name = "谢谢惠顾"
                    
                    for award in awards:
                        cumulative += award["prob"]
                        if r < cumulative:
                            prize = award["amount"]
                            prize_name = award["name"]
                            break
                    
                    total_win += prize
                    win_details[prize_name] = win_details.get(prize_name, 0) + 1
                    
                user["coins"] += total_win
                
                # 构建结果消息
                msg = f"🎰 连续刮开了 {count} 张 {item['name']} ...\n"
                msg += f"💰 总计获得：{total_win} 金币\n"
                msg += "📊 获奖统计：\n"

                # 按照奖项金额排序展示
                sorted_details = sorted(win_details.items(), key=lambda x: next((a['amount'] for a in awards if a['name'] == x[0]), 0), reverse=True)
                
                for name, num in sorted_details:
                    amount = next((a['amount'] for a in awards if a['name'] == name), 0)
                    msg += f"   - {name}({amount}): {num}次\n"
            
            # ========== 其他道具 (通常不支持批量使用，或者循环执行) ==========
            elif item_id == "101": # 精力药水
                if count > 1:
                   msg = "❌ 此道具一次只能使用 1 个。"
                   consumed = False
                else:
                    user["cooldowns"] = {}
                    # 重置所有宠物的冷却
                    for pet_id in user.get("pets", []):
                        pet_data = self._get_user_data(group_id, pet_id)
                        pet_data["cooldowns"] = {}
                        self._save_user_data(group_id, pet_id, pet_data)

                    msg = "🧪 精力焕发！所有冷却时间（含宠物训练）已重置！"
                
            elif item_id == "102": # 护身符
                msg = f"🧿 护身符无需主动使用，放在背包自动生效。(当前库存: {inventory.get(item_id)} 个)"
                consumed = False 
                
            elif item_id == "105": # 宠物零食
                # 零食可以批量喂
                pets = user.get("pets", [])
                if not pets:
                    msg = "❌ 你没有宠物可以喂食。"
                    consumed = False
                else:
                    target_pet_id = pets[0] 
                    pet_data = self._get_user_data(group_id, target_pet_id)
                    
                    total_increase = 0
                    for _ in range(count):
                        total_increase += random.randint(20, 50)
                        
                    pet_data["value"] += total_increase
                    pet_name = pet_data.get("nickname") or f"宠物{target_pet_id}"
                    self._save_user_data(group_id, target_pet_id, pet_data)
                    msg = f"🦴 给 {pet_name} 喂了 {count} 份零食，身价共增加 {total_increase}！"
            
            elif item_id == "107": # 基因药剂
                pets = user.get("pets", [])
                if not pets:
                    msg = "❌ 你没有宠物可以改造。"
                    consumed = False
                else:
                    target_pet_id = pets[0]
                    pet_data = self._get_user_data(group_id, target_pet_id)
                    old_value = pet_data.get("value", 100)
                    pet_name = pet_data.get("nickname") or f"宠物{target_pet_id}"
                    
                    results = []
                    success_count = 0
                    fail_count = 0
                    
                    # 批量使用逻辑
                    new_val_temp = old_value
                    for _ in range(count):
                         if random.random() < 0.3: # 30% 成功
                             increase = int(new_val_temp * 1.0) # +100%
                             new_val_temp += increase
                             success_count += 1
                         else: # 70% 失败
                             decrease = int(new_val_temp * 0.5) # -50%
                             new_val_temp -= decrease
                             fail_count += 1
                    
                    new_val_temp = max(1, new_val_temp) # 最低保留1
                    pet_data["value"] = new_val_temp
                    self._save_user_data(group_id, target_pet_id, pet_data)
                    
                    change = new_val_temp - old_value
                    icon = "📈" if change >= 0 else "📉"
                    msg = (f"💉 对 {pet_name} 进行了 {count} 次基因改造...\n"
                           f"✅ 成功翻倍: {success_count} 次\n"
                           f"❌ 失败变异: {fail_count} 次\n"
                           f"{icon} 身价变化: {old_value} -> {new_val_temp} ({change:+})")

            elif item_id == "108": # 潘多拉魔盒
                # 不支持批量太高风险，或者循环处理
                logs = []
                final_change = 0
                
                for i in range(count):
                    r = random.random()
                    effect_msg = ""
                    if r < 0.08: # 8% 10倍大奖 (2000 -> 20000)
                        prize = 20000
                        user["coins"] += prize
                        effect_msg = "🎆 触发传说级宝藏！获得 20,000 金币 (10倍)！"
                        final_change += prize
                    elif r < 0.30: # 22% 2倍小奖 (2000 -> 4000)
                        prize = 4000
                        user["coins"] += prize
                        effect_msg = "🎉 运气不错！获得 4,000 金币！"
                        final_change += prize
                    elif r < 0.60: # 30% 坐牢
                        jail_time = 4 * 3600 # 4小时
                        user["jailed_until"] = max(user.get("jailed_until", 0), int(time.time())) + jail_time
                        user["jailed_reason"] = "打开潘多拉魔盒释放了恶魔"
                        effect_msg = "👮 盒子释放出恶魔，抓你坐牢 4 小时！"
                    elif r < 0.80: # 20% 破产/失窃 (扣30%)
                        loss = int(user["coins"] * 0.3)
                        user["coins"] -= loss
                        effect_msg = f"💸 盒子是个黑洞，吸走了你 30% 资金 (-{loss}币)！"
                        final_change -= loss
                    else: # 20% 空
                        effect_msg = "💨 盒子里什么都没有，只有一阵嘲笑声..."
                    
                    if count == 1:
                        msg = f"📦 打开潘多拉魔盒...\n{effect_msg}"
                    else:
                        logs.append(effect_msg)

                self._save_user_data(group_id, user_id, user)
                if count > 1:
                   msg = f"📦 连续打开 {count} 个魔盒...\n" + "\n".join([f"{idx+1}. {l}" for idx, l in enumerate(logs)])
                   msg += f"\n💰 总资金变动: {final_change:+}"

            elif item_id == "109": # 走私货物
                total_profit = 0
                success_num = 0
                fail_num = 0
                
                for _ in range(count):
                    # 成本已在购买时扣除(5000)，这里只结算卖出
                    # 售价期望：
                    # 50% 卖出 8000-12000 (均10000) -> 赚5000
                    # 50% 被抓 罚款 2000 -> 亏损购买成本5000+罚款2000 = -7000
                    if random.random() < 0.5:
                        sale_price = random.randint(8000, 12000)
                        user["coins"] += sale_price
                        total_profit += (sale_price) # 这里计算的是回款，算纯利不好算因为购买分离开了，只显示回款和罚款
                        success_num += 1
                    else:
                        fine = 2000
                        user["coins"] = max(0, user.get("coins", 0) - fine)
                        total_profit -= fine # 负数代表扣款
                        fail_num += 1
                        
                self._save_user_data(group_id, user_id, user)
                
                net_income = total_profit
                cost = 5000 * count
                pure_profit = net_income - cost # 算上购买成本的净利润（购买时已扣除，这里net_income是卖出得钱-罚款）
                                                # 修正逻辑：total_profit在成功时加的是全额售价，失败时减的是额外罚款
                                                # 所以 pure_profit = (卖出总回款 - 罚款总额) - 投入成本
                
                # 重新计算一下为了展示清晰
                # 成功：获得 sale_price (包含回本)
                # 失败：失去 fine (不包含回本，通过 buying cost 体现亏损)
                
                real_gain = 0
                for _ in range(success_num): real_gain += 10000 # 估算显示
                real_loss_fine = fail_num * 2000
                
                net_change_now = total_profit # 现在的金币变化（+卖出款 -罚款）

                msg = (f"💼 进行了 {count} 次走私交易...\n"
                       f"✅ 交易成功: {success_num} 次 (高价售出)\n"
                       f"🚓 被捕没收: {fail_num} 次 (货物被缴且罚款)\n"
                       f"💰 资金变动: {net_change_now:+} 金币 (不含进货成本)")
            
            else:
                msg = "❌ 该道具无法主动使用。"
                consumed = False

            if consumed:
                inventory[item_id] -= count
                if inventory[item_id] <= 0:
                    del inventory[item_id]
                self._save_user_data(group_id, user_id, user)

            coins_after = user.get("coins", 0)
            if coins_after != coins_before:
                msg = msg.rstrip()
                msg += f"\n{self._format_amount_change(coins_before, coins_after, '💵 余额')}"
            yield event.plain_result(msg)

    # ==================== 命令：每日签到 ====================
    @filter.command("宠物签到", alias={"签到"})
    async def daily_checkin(self, event: AstrMessageEvent):
        """每日签到领取奖励"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            coins_before = user.get("coins", 0)
            
            last_checkin = user.get("last_checkin", 0)
            now = int(time.time())
            
            # 检查是否是同一天
            import datetime
            last_date = datetime.datetime.fromtimestamp(last_checkin).date()
            current_date = datetime.datetime.fromtimestamp(now).date()
            
            if last_date == current_date:
                yield event.plain_result("📅 今天已经签到过了，明天再来吧！")
                return
                
            # 发放奖励
            coins = random.randint(200, 500)
            user["coins"] += coins
            user["last_checkin"] = now

            msg = f"✅ 签到成功！\n💰 获得金币：{coins}"
            
            # 20% 概率额外获得道具 (随机选一个)
            if random.random() < 0.2:
                item_id = random.choice(list(SHOP_ITEMS.keys()))
                item = SHOP_ITEMS[item_id]
                inventory = user.setdefault("inventory", {})
                inventory[item_id] = inventory.get(item_id, 0) + 1
                msg += f"\n🎁 幸运爆棚！额外获得：{item['icon']} {item['name']} x1"

            msg += f"\n{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            self._save_user_data(group_id, user_id, user)
            yield event.plain_result(msg)

    # ==================== 命令：福利彩票（双色球） ====================
    @filter.command("福利彩票", alias={"彩票", "双色球"})
    async def welfare_lottery(self, event: AstrMessageEvent):
        """双色球彩票：红球1-33选6，蓝球1-16选1"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        args = event.message_str.split()
        
        help_msg = (
            "🎰 【福利彩票】玩法说明\n"
            "------------------\n"
            "规则：6个红球(1-33) + 1个蓝球(1-16)\n"
            "售价：200 金币/注\n"
            "指令：\n"
            "1. 机选：/福利彩票 机选 [注数]\n"
            "2. 自选：/福利彩票 1 5 10 15 20 25 8 (最后一位是蓝球)\n"
            "------------------\n"
            "🏆 奖级赔率（即买即开）：\n"
            "一等奖 (6+1): 10000倍 (200万)\n"
            "二等奖 (6+0): 1000倍 (20万)\n"
            "三等奖 (5+1): 150倍 (3万)\n"
            "四等奖 (5+0, 4+1): 10倍 (2000)\n"
            "五等奖 (4+0, 3+1): 5倍 (1000)\n"
            "六等奖 (1+1, 2+1, 0+1): 3倍 (600)\n" # 保本微赚
        )
        
        if len(args) < 2:
            yield event.plain_result(help_msg)
            return

        # 获取购买参数
        buy_mode = "manual"
        numbers = []
        count = 1
        
        if args[1] == "机选":
            buy_mode = "auto"
            if len(args) > 2 and args[2].isdigit():
                count = int(args[2])
                if count < 1 or count > 50:
                    yield event.plain_result("❌ 单次机选最多 50 注。")
                    return
        else:
            # 解析自选号码
            try:
                nums = [int(x) for x in args[1:] if x.isdigit()]
                if len(nums) != 7:
                    yield event.plain_result("❌ 自选号码必须是 7 个数字（6红+1蓝）。")
                    return
                
                reds = nums[:6]
                blue = nums[6]
                
                if len(set(reds)) != 6:
                    yield event.plain_result("❌ 红球号码不能重复。")
                    return
                
                if any(r < 1 or r > 33 for r in reds) or (blue < 1 or blue > 16):
                    yield event.plain_result("❌ 号码范围错误：红球1-33，蓝球1-16。")
                    return
                    
                numbers = [sorted(reds), blue]
                count = 1 # 自选目前只支持1注
                
            except ValueError:
                yield event.plain_result(help_msg)
                return

        price = 200
        total_cost = price * count

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            coins_before = user.get("coins", 0)
            if user.get("coins", 0) < total_cost:
                yield event.plain_result(f"❌ 余额不足！需要 {total_cost} 金币。")
                return
            
            user["coins"] -= total_cost
            
            results = [] # 记录每一注的结果
            total_prize = 0
            
            # 开奖逻辑
            # 为了公平，每一注都独立开奖一次（即时开奖模式）
            # 生成中奖号码
            def generate_win_num():
                win_reds = sorted(random.sample(range(1, 34), 6))
                win_blue = random.randint(1, 16)
                return win_reds, win_blue
                
            win_reds_final, win_blue_final = generate_win_num()
            
            # 如果是机选，生成用户号码
            user_bets = []
            if buy_mode == "auto":
                for _ in range(count):
                    r_bets = sorted(random.sample(range(1, 34), 6))
                    b_bet = random.randint(1, 16)
                    user_bets.append((r_bets, b_bet))
            else:
                user_bets.append((numbers[0], numbers[1]))
                
            # 统计结果
            win_detail = {} # 统计各奖级数量
            
            for u_reds, u_blue in user_bets:
                # 匹配红球
                hit_red = len(set(u_reds) & set(win_reds_final))
                # 匹配蓝球
                hit_blue = 1 if u_blue == win_blue_final else 0
                
                prize_mult = 0
                rank_name = "未中奖"
                
                if hit_red == 6 and hit_blue == 1:
                    prize_mult = 10000
                    rank_name = "一等奖"
                elif hit_red == 6:
                    prize_mult = 1000
                    rank_name = "二等奖"
                elif hit_red == 5 and hit_blue == 1:
                    prize_mult = 150
                    rank_name = "三等奖"
                elif (hit_red == 5) or (hit_red == 4 and hit_blue == 1):
                    prize_mult = 10
                    rank_name = "四等奖"
                elif (hit_red == 4) or (hit_red == 3 and hit_blue == 1):
                    prize_mult = 5
                    rank_name = "五等奖"
                elif hit_blue == 1: # 只要蓝球中就算六等奖
                    prize_mult = 3
                    rank_name = "六等奖"
                    
                award = price * prize_mult
                total_prize += award
                
                if award > 0:
                    win_detail[rank_name] = win_detail.get(rank_name, 0) + 1
            
            user["coins"] += total_prize
            self._save_user_data(group_id, user_id, user)
            
            # 构建回复
            msg = [
                f"🎰 【福利彩票开奖】 (花费 {total_cost})",
                f"🔴 本期红球：{win_reds_final}",
                f"🔵 本期蓝球：{win_blue_final}",
                "-" * 20
            ]
            
            if count == 1:
                # 单注显示详细匹配
                 u_r, u_b = user_bets[0]
                 msg.append(f"你的号码：{u_r} + {u_b}")
                 if total_prize > 0:
                     msg.append(f"🎉 恭喜中奖！获得 {total_prize} 金币！")
                 else:
                     msg.append("💔 很遗憾，未能中奖。")
            else:
                # 多注显示统计
                msg.append(f"📊 投注 {count} 注，共中奖 {sum(win_detail.values())} 注")
                if total_prize > 0:
                    for k, v in win_detail.items():
                        msg.append(f"   - {k}: {v} 注")
                    msg.append(f"💰 总计奖金：{total_prize} 金币")
                else:
                    msg.append("💔 全军覆没，本次未中奖。")

            msg.append(self._format_amount_change(coins_before, user["coins"], "💵 余额"))
            yield event.plain_result("\n".join(msg))

    @filter.command("管理员发金币")
    async def admin_give_coins(self, event: AstrMessageEvent):
        """管理员给指定用户发钱"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        target_id = self._extract_target(event)
        amount = self._extract_amount(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定用户。")
            return

        if not amount or amount <= 0 or amount > 100000:
            yield event.plain_result("❌ 请指定有效金额（1-100000）。用法: /管理员发金币 @用户 金额")
            return

        group_id = str(event.message_obj.group_id)
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{target_id}"):
            target = self._get_user_data(group_id, target_id)
            target["coins"] = target.get("coins", 0) + amount
            self._save_user_data(group_id, target_id, target)
            target_name = target.get("nickname") or await self._fetch_nickname(event, target_id)
            yield event.plain_result(f"✅ 已向 {target_name} 发放 {amount} 金币。")

    @filter.command("手动清理")
    async def manual_cleanup(self, event: AstrMessageEvent):
        """管理员手动清理群数据"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        group_id = str(event.message_obj.group_id)
        pets = self._get_pets_in_group(group_id)
        removed = len(pets)

        self.pet_data[group_id] = {}
        self._mark_dirty()
        self._save_data()  # 立即保存

        yield event.plain_result(f"✅ 已清空本群所有数据，共 {removed} 条。")

    @filter.command("释放监狱")
    async def release_jail(self, event: AstrMessageEvent):
        """管理员释放指定用户出监狱"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        target_id = self._extract_target(event)
        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定用户。")
            return

        group_id = str(event.message_obj.group_id)
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{target_id}"):
            target = self._get_user_data(group_id, target_id)
            target["jailed_until"] = 0
            self._save_user_data(group_id, target_id, target)
            target_name = target.get("nickname") or await self._fetch_nickname(event, target_id)
            yield event.plain_result(f"✅ 已释放 {target_name} 出监狱。")

    # ==================== 命令：金融市场 ====================
    @filter.command("金融帮助")
    async def market_help(self, event: AstrMessageEvent):
        """查看金融市场操作指南"""
        lines = ["📊 【金融市场操作指南】",
                 "/金融市场 - 查看大盘行情",
                 "/买入 [代码] [金额] - 买入理财产品",
                 "/卖出 [代码] [全部/金额] - 卖出变现",
                 "/我的持仓 - 查看持有盈亏",
                 "",
                 "💡 提示：投资有风险，入市需谨慎！"]
        yield event.plain_result("\n".join(lines))

    @filter.command("金融市场", alias={"大盘", "股市"})
    async def market_view(self, event: AstrMessageEvent):
        """查看金融市场大盘"""
        # 触发一次更新检查（如果太久没更新）
        if int(time.time()) - self.market_manager.market_data["last_update"] > 1800:
            self.market_manager.update_market()
            
        summary = self.market_manager.get_market_summary()
        yield event.plain_result(summary)

    # ==================== 命令：买入 ====================
    @filter.command("买入", alias={"投资"})
    async def buy_instrument(self, event: AstrMessageEvent):
        """买入理财产品：/买入 [代码] [金额]"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /买入 [代码] [金额] (例如: /买入 F101 1000)\n可在 /金融市场 查看代码")
            return
            
        # 尝试解析参数，兼容 /买入 1000 F101 和 /买入 F101 1000
        code = None
        amount = 0
        
        for arg in args:
            if arg.isdigit():
                amount = int(arg)
            else:
                code, _ = self.market_manager.get_instrument(arg)
        
        if not code:
            yield event.plain_result("❌ 未找到该代码的产品，请检查输入。")
            return
            
        if amount < 100:
            yield event.plain_result("❌ 最低买入金额为 100 金币。")
            return

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法进行交易。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            if user.get("coins", 0) < amount:
                yield event.plain_result(f"❌ 余额不足！需 {amount}，余额 {user['coins']}。")
                return

            # 处理旧版投资清理
            if "investments" in user and user["investments"]:
                yield event.plain_result("⚠️ 检测到旧版投资数据，系统升级，正在为您结算旧版投资...")
                old_invs = user.pop("investments")
                refund = 0
                for inv in old_invs:
                    if inv["status"] == "active":
                         refund += inv["current_value"]
                if refund > 0:
                    user["coins"] += refund
                    yield event.plain_result(f"✅ 旧投资已结算，返还 {refund} 金币。请重新操作。")
                    self._save_user_data(group_id, user_id, user)
                    return # 让用户重新买，避免逻辑混乱

            # 初始化新版持仓结构
            if "holdings" not in user:
                user["holdings"] = {}

            # 扣款
            coins_before = user.get("coins", 0)
            user["coins"] -= amount
            
            # 记录持仓
            _, data = self.market_manager.get_instrument(code)
            current_price = data["current_price"]
            buy_shares = amount / current_price
            
            holding = user["holdings"].get(code, {"shares": 0.0, "total_cost": 0.0, "avg_price": 0.0})
            holding["shares"] += buy_shares
            holding["total_cost"] += amount
            holding["avg_price"] = holding["total_cost"] / holding["shares"]
            user["holdings"][code] = holding

            self._save_user_data(group_id, user_id, user)

            yield event.plain_result(
                f"✅ 买入成功！\n"
                f"📄 产品：{data['name']} ({code})\n"
                f"💰 投入：{amount} 金币\n"
                f"📊 份额：{buy_shares:.4f}\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    # ==================== 命令：卖出 ====================
    @filter.command("卖出", alias={"赎回"})
    async def sell_instrument(self, event: AstrMessageEvent):
        """卖出理财产品：/卖出 [代码] [全部/金额]"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("❌ 用法: /卖出 [代码] [全部/份额/金额]")
            return

        target_code_input = None
        sell_amount_input = None
        is_sell_all = False

        for arg in args:
            if arg in ["全部", "all", "ALL"]:
                is_sell_all = True
            elif arg.replace('.', '', 1).isdigit(): # is float or int
                sell_amount_input = float(arg) # 可能是金额也可能是份额，这里简化为只支持卖出金额或者“全部”
            else:
                target_code_input = arg

        code, instrument_data = self.market_manager.get_instrument(target_code_input) if target_code_input else (None, None)

        if not code:
             yield event.plain_result("❌ 请指定正确的产品代码。")
             return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            holdings = user.get("holdings", {})
            
            if code not in holdings:
                yield event.plain_result(f"❌ 你没有持有 {instrument_data['name']}。")
                return

            holding = holdings[code]
            current_price = instrument_data["current_price"]
            max_value = holding["shares"] * current_price
            
            sell_value = 0
            sell_shares = 0

            if is_sell_all:
                sell_shares = holding["shares"]
                sell_value = max_value
            elif sell_amount_input is not None:
                # 假设输入的是金额（为了方便用户）
                if not sell_amount_input.is_integer():
                    yield event.plain_result("❌ 卖出金额必须是整数金币。")
                    return
                sell_value = int(sell_amount_input)
                if sell_value <= 0:
                    yield event.plain_result("❌ 卖出金额必须大于 0。")
                    return
                if sell_value > max_value:
                    yield event.plain_result(f"❌ 持仓价值不足，当前仅有 {max_value:.2f} 金币。")
                    return
                sell_shares = sell_value / current_price
            else:
                yield event.plain_result("❌ 请输入要卖出的金额或“全部”。")
                return

            # 执行卖出
            holding["shares"] -= sell_shares
            cost_basis = holding["avg_price"] * sell_shares
            payout = int(sell_value)
            profit = payout - cost_basis

            coins_before = user.get("coins", 0)
            user["coins"] += payout
            # 更新成本 (总量变少，单价不变)
            holding["total_cost"] -= cost_basis
            
            if holding["shares"] < 0.0001:
                del holdings[code]
            
            self._save_user_data(group_id, user_id, user)
            
            yield event.plain_result(
                f"✅ 卖出成功！\n"
                f"📄 产品：{instrument_data['name']}\n"
                f"💰 获得资金：{payout} 金币\n"
                f"📈 盈亏：{profit:+.2f} 金币\n"
                f"{self._format_amount_change(coins_before, user['coins'], '💵 余额')}"
            )

    # ==================== 命令：我的持仓 ====================
    @filter.command("我的持仓", alias={"持仓"})
    async def my_portfolio(self, event: AstrMessageEvent):
        """查看当前持仓详情"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        
        # 触发更新
        if int(time.time()) - self.market_manager.market_data["last_update"] > 1800:
            self.market_manager.update_market()

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            holdings = user.get("holdings", {})
            
            if not holdings:
                yield event.plain_result("👜 你当前没有持有任何理财产品。\n使用 /金融市场 查看行情，/买入 进行投资。")
                return

            lines = ["👜 【我的持仓元宇宙】"]
            total_market_value = 0
            total_profit = 0
            
            for code, data in holdings.items():
                _, info = self.market_manager.get_instrument(code)
                if not info: continue
                
                current_price = info["current_price"]
                market_value = data["shares"] * current_price
                cost = data["total_cost"]
                profit = market_value - cost
                profit_rate = profit / cost if cost > 0 else 0
                
                total_market_value += market_value
                total_profit += profit
                
                icon = "🔴" if profit >= 0 else "🟢" # 红涨绿跌（A股习惯），或者通用 📈 📉
                icon = "📈" if profit >= 0 else "📉"
                
                lines.append(f"{icon} {info['name']} ({code})")
                lines.append(f"   持有: {data['shares']:.2f} 份 | 市值: {int(market_value)}")
                lines.append(f"   盈亏: {profit:+.2f} ({profit_rate:+.2%})")
            
            lines.append("-" * 20)
            lines.append(f"💰 总市值: {int(total_market_value)} 金币")
            lines.append(f"💸 总盈亏: {total_profit:+.2f} 金币")
            
            yield event.plain_result("\n".join(lines))
