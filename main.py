import os
import yaml
import random
import time
import json
import asyncio
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


# ==================== 主类 ====================
class Main(Star):
    def __init__(self, context: Context, **kwargs):
        super().__init__(context)
        self.context = context
        self.config = context._config
        self.pet_data: Dict = {}
        self.copywriting: Dict = {}
        self.train_copywriting: Dict = {}
        self._dirty = False  # 脏数据标记
        self._save_task: Optional[asyncio.Task] = None

        # 【规范化】使用 get_astrbot_data_path 获取标准数据目录
        # 符合 astrbot 规范：data/plugin_data/{plugin_name}/
        global DATA_DIR, DATA_FILE, BACKUP_DIR
        plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        DATA_DIR = plugin_data_path
        DATA_FILE = DATA_DIR / "pet_data.yml"
        BACKUP_DIR = DATA_DIR / "backups"

        # 【新增】初始化管理员列表
        self.admins = self._init_admins()

        self._init_env()
        self._load_data()
        self._load_copywriting()

    # ==================== 生命周期管理 ====================
    async def initialize(self):
        """插件初始化"""
        logger.info("[宠物市场] 插件初始化")
        # 启动自动保存任务
        self._save_task = asyncio.create_task(self._auto_save_loop())

    async def terminate(self):
        """插件终止"""
        logger.info("[宠物市场] 插件正在关闭")
        # 取消自动保存任务
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        # 最终保存数据
        if self._dirty:
            self._save_data()
        logger.info("[宠物市场] 插件已关闭")

    async def _auto_save_loop(self):
        """自动保存循环（每60秒，异步执行避免阻塞）"""
        try:
            while True:
                await asyncio.sleep(60)
                if self._dirty:
                    await self._save_data_async()
                    self._dirty = False
                    logger.debug("[宠物市场] 自动保存完成")
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
            self._try_restore_backup()
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
        # 创建数据副本避免并发问题
        data_copy = dict(self.pet_data)
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
            self._dirty = True
            logger.info(f"[宠物市场] 新用户 {user_id} 初始化，发放 {INITIAL_COINS} 金币")
        return group_data[user_id]

    def _save_user_data(self, group_id: str, user_id: str, data: Dict):
        """保存用户数据（仅标记脏数据）"""
        data["last_active"] = int(time.time())
        self.pet_data.setdefault(group_id, {})[user_id] = data
        self._dirty = True

    def _get_pets_in_group(self, group_id: str) -> Dict:
        """获取群内所有宠物数据"""
        return self.pet_data.get(group_id, {})

    def _remove_user_data(self, group_id: str, user_id: str):
        """删除用户数据"""
        self.pet_data.get(group_id, {}).pop(user_id, None)
        self._dirty = True

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
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)

        # 从文字提取QQ号（仅在没有@时使用）
        # 注意：为避免与金额等数字混淆，仅匹配消息末尾的QQ号
        import re
        # 匹配消息末尾的5-11位数字（QQ号范围）
        match = re.search(r'(\d{5,11})\s*$', event.message_str)
        return match.group(1) if match else None

    def _extract_amount(self, event: AstrMessageEvent) -> Optional[int]:
        """从消息中提取金额数字"""
        import re
        # 将金额上限从4位提升到8位，以支持更大的贷款和转账
        match = re.search(r'\b(\d{1,8})\b', event.message_str)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

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
            # 1. 计算理论上的复利后总金额
            theoretical_loan = int(loan_total * ((1 + rate) ** hours))

            # 2. 计算封顶金额 = 本金 + 本金*倍率
            max_loan = int(principal * (1 + max_multiplier))

            # 3. 比较，取较小值
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
                {"cmd": "/投资 5000", "desc": "💰 进行主投资（最低5000）"},
                {"cmd": "/加投 500", "desc": "📈 在现有投资上加投（500-5000）"},
                {"cmd": "/投资状态", "desc": "查看当前投资状态与收益"},
                {"cmd": "/止盈", "desc": "主动止盈获取收益"},
                {"cmd": "/止损", "desc": "主动止损减少亏损"},
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
            for item in menu_data["items"]:
                text_menu += f"{item['cmd']}\n  └─ {item['desc']}\n\n"
            text_menu += "💡 提示：图片菜单生成失败，显示文本版本"
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
                        f"⭐ 当前阶段：{target_data['evolution_stage']}"
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
                        f"⭐ 当前阶段：{target_data['evolution_stage']}"
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
                f"💵 当前余额：{user_data['coins']} 金币"
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
                        lines.append(f"[{stage}] {name}：{copywriting} -{loss}")
                        self._save_user_data(group_id, pid, pet)

            # 【新增】打工纳税逻辑
            master_id = user_data.get("master", "")
            tax_rate = self.config.get("work_tax_rate", 0.3)

            if master_id and total > 0:
                tax = int(total * tax_rate)
                net_income = total - tax

                # 给主人加钱
                master_data = self._get_user_data(group_id, master_id)
                master_data["coins"] = master_data.get("coins", 0) + tax
                self._save_user_data(group_id, master_id, master_data)

                master_name = master_data.get("nickname") or await self._fetch_nickname(event, master_id)

                user_data["coins"] = user_data.get("coins", 0) + net_income
                lines.append(f"\n💸 上交主人({master_name}) {int(tax_rate * 100)}%：{tax} 金币")
                lines.append(f"💰 实得收入：{net_income} 金币")
            else:
                user_data["coins"] = user_data.get("coins", 0) + total
                lines.append(f"\n💰 总计获得 {total} 金币")

            # 【新增】检查投资结算
            investment_msgs = self._settle_investments(user_data)
            
            self._set_cooldown(user_data, "work")
            self._save_user_data(group_id, user_id, user_data)

            lines.append(f"💵 当前余额：{user_data['coins']} 金币")
            
            # 添加投资信息
            if investment_msgs:
                lines.append("")
                for msg in investment_msgs:
                    lines.append(msg)
            
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
                    yield event.plain_result(f"✅ {msg}\n⭐ 当前阶段：{pet['evolution_stage']}")
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
                    yield event.plain_result(f"❌ {msg}\n⭐ 当前阶段：{pet['evolution_stage']}")

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
                f"💵 当前余额：{user_data['coins']} 金币"
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
            summary += f"💵 当前余额：{user_data['coins']} 金币\n\n"
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
                        f"💵 当前余额：{user_data['coins']} 金币"
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
                        f"💵 当前余额：{user_data['coins']} 金币"
                    )

    # ==================== 命令：我的宠物 ====================
    @filter.command("我的宠物")
    async def my_pets(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        # 加入锁机制以检测爆仓
        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            # 更新利息并检查强制清算
            self._update_loan_interest(user)
            if await self._check_and_liquidate(event, group_id, user_id, user):
                return

            self._save_user_data(group_id, user_id, user)

            pets = user.get("pets", [])
            lines = ["【🐾 我的宠物】"]

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

            lines.append(f"\n💵 当前余额：{coins} 金币")
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

            user["coins"] -= cost
            user["bank_level"] = level + 1
            self._save_user_data(group_id, user_id, user)
            new_limit = self._get_bank_limit(user["bank_level"])

            yield event.plain_result(
                f"✅ 升级成功！信用等级提升至 Lv.{user['bank_level']}\n"
                f"📦 新存储上限：{new_limit} 金币\n"
                f"💰 消耗 {cost} 金币，当前余额 {user['coins']} 金币"
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
            user["coins"] = user.get("coins", 0) + interest
            self._save_user_data(group_id, user_id, user)

            yield event.plain_result(
                f"✅ 成功领取利息 {interest} 金币到余额。\n"
                f"⏰ 计息时长：{hours} 小时\n"
                f"💵 当前余额：{user['coins']} 金币\n"
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

            yield event.plain_result(
                f"✅ 存款成功！存入 {amount} 金币。\n"
                f"💵 当前余额：{user['coins']} 金币\n"
                f"🏦 当前存款：{user['bank']} 金币\n"
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

            if user.get("bank", 0) < amount:
                yield event.plain_result("❌ 银行存款不足。")
                return

            user["bank"] -= amount
            user["coins"] = user.get("coins", 0) + amount
            self._save_user_data(group_id, user_id, user)

            yield event.plain_result(
                f"✅ 取款成功！取出 {amount} 金币。\n"
                f"💵 当前余额：{user['coins']} 金币\n"
                f"🏦 当前存款：{user['bank']} 金币"
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

            user["loan_amount"] = current_loan + amount
            user["coins"] = user.get("coins", 0) + amount
            user["loan_principal"] = user.get("loan_principal", 0) + amount

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 贷款成功！获得 {amount} 金币。\n"
            msg += f"💸 当前欠款：{user['loan_amount']} (本金 {user['loan_principal']})\n"
            msg += f"💵 当前余额：{user['coins']} 金币\n"
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

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 还款成功！支付 {real_repay} 金币。\n"
            msg += f"💸 剩余欠款：{user['loan_amount']} (本金 {user['loan_principal']})\n"
            msg += f"💵 当前余额：{user['coins']} 金币"

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

                self._save_user_data(group_id, user_id, user_data)
                self._save_user_data(group_id, target_id, target_data)

                user_name = user_data.get("nickname") or await self._fetch_nickname(event, user_id)

                target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)

                yield event.plain_result(
                    f"✅ 转账成功！\n"
                    f"💸 从 {user_name} 转给 {target_name}\n"
                    f"💰 转账金额：{amount} 金币\n"
                    f"💵 手续费：{fee} 金币 ({int(fee_rate * 100)}%)\n"
                    f"📊 你的余额：{user_data['coins']} 金币\n"
                    f"📊 对方余额：{target_data['coins']} 金币"
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
                        f"💵 当前余额：{user_data['coins']} 金币"
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

                    yield event.plain_result(
                        f"🚨 抢劫失败！{user_name} 被当场抓获！\n"
                        f"⚖️ 当前连败次数：{streak} (罚款倍率 {multiplier}x)\n"
                        f"💸 罚款金额：{fine} 金币 (按身价计算)\n"
                        f"⚠️ 请在以下选项中二选一：\n"
                        f"1. 发送 /交罚款 (扣除金币，保留自由)\n"
                        f"2. 发送 /坐牢 (无需罚款，监禁24小时)"
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

            user_data["coins"] -= fine
            user_data["rob_pending_penalty"] = None  # 清除状态
            user_data["rob_fail_streak"] += 1  # 增加连败次数，下次更贵

            self._save_user_data(group_id, user_id, user_data)
            yield event.plain_result(f"💸 罚款缴纳成功！扣除 {fine} 金币。下次抢劫失败罚款倍率将提升。")

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
        admins = []
        
        # 方式1：从 config 中的 admin_uins 字段获取
        admin_list = self.config.get("admin_uins", [])
        if admin_list:
            for admin_id in admin_list:
                admin_str = str(admin_id).strip()
                if admin_str.isdigit():
                    admins.append(admin_str)
                    logger.debug(f"[宠物市场] 添加管理员: {admin_str} (来自 admin_uins)")
        
        # 方式2：从 context 的全局配置中获取 admins_id
        try:
            global_config = self.context.get_config()
            if global_config and isinstance(global_config, dict):
                admins_id = global_config.get("admins_id", [])
                if admins_id:
                    for admin_id in admins_id:
                        admin_str = str(admin_id).strip()
                        if admin_str.isdigit() and admin_str not in admins:
                            admins.append(admin_str)
                            logger.debug(f"[宠物市场] 添加管理员: {admin_str} (来自 admins_id)")
        except Exception as e:
            logger.warning(f"[宠物市场] 从全局配置获取管理员失败: {e}")
        
        # 如果没有配置任何管理员，使用默认管理员
        if not admins:
            admins = ["846994183", "3864670906"]
            logger.info(f"[宠物市场] 使用默认管理员列表: {admins}")
        else:
            logger.info(f"[宠物市场] 已加载 {len(admins)} 个管理员: {admins}")
        
        return admins

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
            user["coins"] = user.get("coins", 0) + amount
            self._save_user_data(group_id, user_id, user)
            yield event.plain_result(f"✅ 已发放 {amount} 金币，当前余额 {user['coins']} 金币。")

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
        self._dirty = True
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

    # ==================== 命令：投资 ====================
    @filter.command("投资")
    async def invest(self, event: AstrMessageEvent):
        """进行主投资（最低5000）"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        amount = self._extract_amount(event)
        if not amount or amount < 5000:
            yield event.plain_result("❌ 投资金额不能少于 5000 金币。用法: /投资 5000")
            return

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法进行投资。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            # 检查是否有未还清的贷款
            current_loan = user.get("loan_amount", 0)
            if current_loan > 0:
                yield event.plain_result(f"❌ 你还有 {current_loan} 金币的未清欠款，必须先还清贷款才能投资！")
                return

            if user.get("coins", 0) < amount:
                yield event.plain_result(f"❌ 余额不足！需投资 {amount}，余额 {user['coins']}。")
                return

            # 检查是否已有活跃投资
            active_investments = [inv for inv in user.get("investments", []) if inv.get("status") == "active"]
            if active_investments:
                yield event.plain_result("❌ 你已有活跃的投资，请先结算或触发止盈/止损！")
                return

            # 创建新投资
            trend_name, change_rate = self._get_investment_trend()
            investment_id = user.get("next_investment_id", 1)
            
            investment = {
                "id": investment_id,
                "type": "main",  # 主投资
                "amount": amount,
                "start_time": int(time.time()),
                "status": "active",
                "current_value": amount,
                "trend_history": [(trend_name, change_rate)],
                "addon_amount": 0,  # 加投金额
                "next_settlement_time": int(time.time()) + 3600  # 1小时后结算
            }

            user["coins"] -= amount
            user["investments"].append(investment)
            user["next_investment_id"] = investment_id + 1
            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 投资成功！\n"
            msg += f"💰 投资金额：{amount} 金币\n"
            msg += f"📈 初始趋势：{trend_name} {change_rate:+.2%}\n"
            msg += f"💵 当前余额：{user['coins']} 金币\n"
            msg += f"⏰ 约24小时后可查看收益"

            yield event.plain_result(msg)

    # ==================== 命令：加投 ====================
    @filter.command("加投")
    async def add_investment(self, event: AstrMessageEvent):
        """在现有投资上加投（500-5000）"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        amount = self._extract_amount(event)
        if not amount or amount < 500 or amount > 5000:
            yield event.plain_result("❌ 加投金额需在 500-5000 之间。用法: /加投 1000")
            return

        jailed, remain = self._check_jailed(group_id, user_id)
        if jailed:
            yield event.plain_result(f"🔒 监狱里无法进行加投。")
            return

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)

            if user.get("coins", 0) < amount:
                yield event.plain_result(f"❌ 余额不足！需加投 {amount}，余额 {user['coins']}。")
                return

            # 检查是否有活跃的主投资
            active_investments = [inv for inv in user.get("investments", []) 
                                if inv.get("status") == "active" and inv.get("type") == "main"]
            if not active_investments:
                yield event.plain_result("❌ 你没有活跃的主投资，无法加投。请先使用 /投资 进行投资。")
                return

            investment = active_investments[0]
            
            # 检查加投总额是否超过5000
            current_addon = investment.get("addon_amount", 0)
            if current_addon + amount > 5000:
                can_add = max(0, 5000 - current_addon)
                yield event.plain_result(f"❌ 加投超限！已加投 {current_addon}，还可加投 {can_add}。")
                return

            # 【修复】执行加投 - 不应立即应用趋势，只增加投资金额
            user["coins"] -= amount
            investment["addon_amount"] += amount
            investment["current_value"] += amount  # 只增加投资金额，下次结算时应用趋势
            # 注意：不应该这里追加趋势历史，应该在结算时追加
            
            self._save_user_data(group_id, user_id, user)

            total_investment = investment["amount"] + investment["addon_amount"]
            msg = f"✅ 加投成功！\n"
            msg += f"💰 加投金额：{amount} 金币\n"
            msg += f"💵 当前总投资：{total_investment} 金币\n"
            msg += f"💵 当前价值：{investment['current_value']} 金币\n"
            msg += f"💵 当前余额：{user['coins']} 金币\n"
            msg += f"⏰ 下次结算时应用加投的收益计算"

            yield event.plain_result(msg)

    # ==================== 命令：投资状态 ====================
    @filter.command("投资状态")
    async def investment_status(self, event: AstrMessageEvent):
        """查看当前投资状态与收益"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            investments = user.get("investments", [])

            if not investments:
                yield event.plain_result("❌ 你还没有任何投资。")
                return

            # 过滤活跃投资
            active_investments = [inv for inv in investments if inv.get("status") == "active"]
            
            if not active_investments:
                yield event.plain_result("❌ 你没有活跃的投资。")
                return

            investment = active_investments[0]
            elapsed = int(time.time()) - investment["start_time"]
            days = elapsed // 86400
            hours = (elapsed % 86400) // 3600
            mins = (elapsed % 3600) // 60

            current_value = investment["current_value"]
            total_input = investment["amount"] + investment.get("addon_amount", 0)
            profit = current_value - total_input
            profit_rate = profit / total_input if total_input > 0 else 0

            # 检查是否触发止盈/止损
            trigger = self._check_investment_trigger(investment)
            
            # 【修复】投资类型判断应该基于是否有加投
            addon_amount = investment.get("addon_amount", 0)
            if addon_amount > 0:
                investment_type_str = f"主投资({investment['amount']}) + 加投({addon_amount})"
            else:
                investment_type_str = f"主投资({investment['amount']})"
            
            msg = f"【📊 投资状态】\n"
            msg += f"投资类型：{investment_type_str}\n"
            msg += f"投入总额：{total_input} 金币\n"
            msg += f"当前价值：{current_value} 金币\n"
            msg += f"收益：{profit:+d} 金币（{profit_rate:+.2%}）\n"
            # 【改进】显示运行时间时包含天数
            if days > 0:
                msg += f"运行时间：{days}天{hours}小时{mins}分钟\n"
            else:
                msg += f"运行时间：{hours}小时{mins}分钟\n"
            msg += f"\n📈 趋势历史：\n"
            
            for i, (trend, rate) in enumerate(investment["trend_history"][-5:], 1):
                msg += f"  {i}. {trend} {rate:+.2%}\n"

            if trigger:
                msg += f"\n🔔 触发条件：{trigger}\n"
                msg += f"💡 可使用 /{trigger} 来执行操作"

            yield event.plain_result(msg)

    # ==================== 命令：止盈 ====================
    @filter.command("止盈")
    async def take_profit(self, event: AstrMessageEvent):
        """主动止盈获取收益"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            # 检查是否有活跃投资
            active_investments = [inv for inv in user.get("investments", []) if inv.get("status") == "active"]
            if not active_investments:
                yield event.plain_result("❌ 你没有活跃的投资。")
                return

            investment = active_investments[0]
            current_value = investment["current_value"]
            total_input = investment["amount"] + investment.get("addon_amount", 0)
            profit = current_value - total_input

            # 执行止盈
            user["coins"] += current_value
            investment["status"] = "closed"
            investment["profit"] = profit
            investment["close_time"] = int(time.time())
            investment["close_reason"] = "止盈"

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 止盈成功！\n"
            msg += f"💰 收回资金：{current_value} 金币\n"
            msg += f"📈 本次收益：{profit:+d} 金币（{profit/total_input if total_input > 0 else 0:+.2%}）\n"
            msg += f"💵 当前余额：{user['coins']} 金币"

            yield event.plain_result(msg)

    # ==================== 命令：止损 ====================
    @filter.command("止损")
    async def stop_loss(self, event: AstrMessageEvent):
        """主动止损减少亏损"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())

        async with session_lock_manager.acquire_lock(f"pet_market_{group_id}_{user_id}"):
            user = self._get_user_data(group_id, user_id)
            
            # 检查是否有活跃投资
            active_investments = [inv for inv in user.get("investments", []) if inv.get("status") == "active"]
            if not active_investments:
                yield event.plain_result("❌ 你没有活跃的投资。")
                return

            investment = active_investments[0]
            current_value = investment["current_value"]
            total_input = investment["amount"] + investment.get("addon_amount", 0)
            loss = current_value - total_input

            # 执行止损
            user["coins"] += current_value
            investment["status"] = "closed"
            investment["loss"] = loss
            investment["close_time"] = int(time.time())
            investment["close_reason"] = "止损"

            self._save_user_data(group_id, user_id, user)

            msg = f"✅ 止损成功！\n"
            msg += f"💰 收回资金：{current_value} 金币\n"
            msg += f"📉 本次亏损：{loss:+d} 金币（{loss/total_input if total_input > 0 else 0:.2%}）\n"
            msg += f"💵 当前余额：{user['coins']} 金币"

            yield event.plain_result(msg)
