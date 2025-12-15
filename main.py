import os
import yaml
import random
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from astrbot.api import star, logger
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.session_lock import session_lock_manager

# ==================== 常量定义 ====================
PLUGIN_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join("data", "pet_market")
DATA_FILE = os.path.join(DATA_DIR, "pet_data.yml")
COPYWRITING_FILE = os.path.join(PLUGIN_DIR, "resources", "data", "pet_copywriting.json")
TRAIN_COPYWRITING_FILE = os.path.join(PLUGIN_DIR, "resources", "data", "train_copywriting.json")
CARD_TEMPLATE = os.path.join(PLUGIN_DIR, "card_template.html")
MENU_TEMPLATE = os.path.join(PLUGIN_DIR, "menu_template.html")

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
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.config = context._config
        self.pet_data: Dict = {}
        self.copywriting: Dict = {}
        self.train_copywriting: Dict = {}
        self._dirty = False  # 脏数据标记
        self._save_task: Optional[asyncio.Task] = None
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
        """自动保存循环（每60秒）"""
        try:
            while True:
                await asyncio.sleep(60)
                if self._dirty:
                    self._save_data()
                    self._dirty = False
                    logger.debug("[宠物市场] 自动保存完成")
        except asyncio.CancelledError:
            logger.debug("[宠物市场] 自动保存任务已取消")
            raise

    # ==================== 数据管理 ====================
    def _init_env(self):
        """初始化环境"""
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump({}, f)

    def _load_data(self):
        """加载数据"""
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                self.pet_data = yaml.safe_load(f) or {}
            logger.info(f"[宠物市场] 数据加载成功，共 {len(self.pet_data)} 个群组")
        except Exception as e:
            logger.error(f"[宠物市场] 数据加载失败: {e}")
            self.pet_data = {}

    def _save_data(self):
        """保存数据到文件"""
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                yaml.dump(self.pet_data, f, allow_unicode=True)
            logger.debug("[宠物市场] 数据保存成功")
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
        if user_id not in group_data:
            # 首次交互，自动发放初始金币
            group_data[user_id] = {
                "coins": INITIAL_COINS,
                "value": 100,
                "pets": [],
                "master": "",
                "nickname": "",
                "cooldowns": {},  # 统一冷却字典
                "bank": 0,
                "bank_level": 1,
                "last_interest_time": int(time.time()),
                "jailed_until": 0,
                "last_active": int(time.time()),
                "initialized": True,
                "transfer_history": [],
                "evolution_stage": "普通"
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
        """提取目标用户ID"""
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        # 从文字提取QQ号
        import re
        match = re.search(r"(\d{5,})", event.message_str)
        return match.group(1) if match else None

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

    # ==================== 命令：宠物菜单 ====================
    @filter.command("宠物菜单")
    async def pet_menu(self, event: AstrMessageEvent):
        """显示功能菜单"""
        menu_data = {
            "title": "🐾 宠物市场菜单",
            "items": [
                {"cmd": "/宠物市场 [页码]", "desc": "查看群内宠物列表（支持分页）"},
                {"cmd": "/购买宠物 @群友/QQ", "desc": "购买指定宠物"},
                {"cmd": "/放生宠物 @群友/QQ", "desc": "放生宠物"},
                {"cmd": "/打工", "desc": "派遣宠物打工赚钱"},
                {"cmd": "/训练 @群友/QQ", "desc": "训练宠物提升身价（冷却1天）"},
                {"cmd": "/进化宠物 @群友/QQ", "desc": "消耗金币进化宠物"},
                {"cmd": "/PK @群友/QQ", "desc": "⚔️ 宠物决斗（赢家掠夺10%身价）"},
                {"cmd": "/我的宠物", "desc": "查看自己的宠物与金币"},
                {"cmd": "/银行信息", "desc": "查看银行等级与利息"},
                {"cmd": "/升级信用", "desc": "提升银行等级与存储上限"},
                {"cmd": "/领取利息", "desc": "领取银行存款利息到余额"},
                {"cmd": "/存款 100", "desc": "存入金币到银行"},
                {"cmd": "/取款 50", "desc": "从银行取出金币"},
                {"cmd": "/转账 @群友/QQ 金额", "desc": "转账给其他玩家"},
                {"cmd": "/转账记录", "desc": "查看最近10条转账记录"},
                {"cmd": "/宠物身价排行榜 [页码]", "desc": "查看身价排行（支持分页）"},
                {"cmd": "/宠物资金排行榜 [页码]", "desc": "查看余额排行（支持分页）"},
                {"cmd": "/群内十大首富 [页码]", "desc": "查看总资产排行（支持分页）"},
                {"cmd": "/抢劫 @群友/QQ", "desc": "每小时可抢劫一次"},
            ]
        }
        try:
            template = self._load_template(MENU_TEMPLATE)
            url = await self.html_render(template, menu_data)
            yield event.image_result(url)
        except Exception as e:
            logger.error(f"[宠物市场] 菜单生成失败: {e}")
            yield event.plain_result("菜单生成失败，请稍后再试。")

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

            user_data["pets"].remove(target_id)
            target_data = self._get_user_data(group_id, target_id)
            target_data["master"] = ""
            self._save_user_data(group_id, user_id, user_data)
            self._save_user_data(group_id, target_id, target_data)
            target_name = target_data.get("nickname") or await self._fetch_nickname(event, target_id)
            yield event.plain_result(f"🕊️ 成功放生宠物 {target_name}。")

    # ==================== 命令：打工 ====================
    @filter.command("打工")
    async def work(self, event: AstrMessageEvent):
        """派遣宠物打工"""
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

            user_data["coins"] = user_data.get("coins", 0) + total
            self._set_cooldown(user_data, "work")
            self._save_user_data(group_id, user_id, user_data)
            lines.append(f"\n💰 总计获得 {total} 金币，当前余额 {user_data['coins']} 金币。")
            yield event.plain_result("\n".join(lines))

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
                    yield event.plain_result(f"❌ {name} 已经是传说阶段，无法继续进化。")
                    return
                else:
                    yield event.plain_result(f"❌ {name} 已经是最高阶段。")
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
                        f"📈 打工收益 +{int(work_bonus*100)}%\n"
                        f"📈 训练成功率 +{int(train_bonus*100)}%\n"
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
        """查看自己的宠物"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user = self._get_user_data(group_id, user_id)
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
        lines.append(f"\n💵 当前余额：{coins} 金币")
        lines.append(f"🏦 银行存款：{bank} 金币 (Lv.{bank_level})")
        lines.append(f"💎 总资产：{coins + bank} 金币")
        
        yield event.plain_result("\n".join(lines))

    # ==================== 命令：银行信息 ====================
    @filter.command("银行信息")
    async def bank_info(self, event: AstrMessageEvent):
        """查看银行信息"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        user = self._get_user_data(group_id, user_id)
        
        bank = user.get("bank", 0)
        level = user.get("bank_level", 1)
        limit = self._get_bank_limit(level)
        rate = self.config.get("bank_interest_rate", 0.01)
        next_cost = self._get_upgrade_cost(level)
        
        # 计算当前可领取利息
        last_interest = user.get("last_interest_time", int(time.time()))
        now = int(time.time())
        hours = min((now - last_interest) // 3600, self.config.get("bank_max_interest_time", 24))
        potential_interest = self._calculate_compound_interest(bank, rate, hours) if bank > 0 else 0
        
        yield event.plain_result(
            f"【🏦 银行信息】\n"
            f"💰 当前存款：{bank} 金币\n"
            f"⭐ 信用等级：Lv.{level}\n"
            f"📦 存储上限：{limit} 金币\n"
            f"📈 每小时利息：{rate * 100}%（复利）\n"
            f"💵 可领利息：{potential_interest} 金币\n"
            f"⬆️ 下次升级费用：{next_cost} 金币"
        )

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

    # ==================== 命令：转账 ====================
    @filter.command("转账")
    async def transfer(self, event: AstrMessageEvent, amount: int):
        """转账给其他玩家"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定转账目标。")
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
                        f"手续费：{fee} ({int(fee_rate*100)}%)\n"
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
                    f"💵 手续费：{fee} 金币 ({int(fee_rate*100)}%)\n"
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
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
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
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
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
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
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
        """抢劫其他玩家"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target_id = self._extract_target(event)

        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定抢劫目标。")
            return

        if target_id == user_id:
            yield event.plain_result("❌ 不能抢劫自己。")
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

                # 检查冷却（使用配置）
                cooldown_seconds = self.config.get("rob_cooldown", 3600)
                in_cooldown, remain = self._check_cooldown(user_data, "rob", cooldown_seconds)
                if in_cooldown:
                    mins = remain // 60
                    yield event.plain_result(f"⏰ 抢劫冷却中，剩余 {mins} 分钟。")
                    return

                if target_data.get("coins", 0) == 0:
                    yield event.plain_result("❌ 目标余额为0，无法抢劫。")
                    return

                self._set_cooldown(user_data, "rob")

                # 计算成功率（基于银行等级）
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
                    self._save_user_data(group_id, user_id, user_data)
                    self._save_user_data(group_id, target_id, target_data)
                    
                    yield event.plain_result(
                        f"💰 抢劫成功！{user_name} 从 {target_name} 手中抢走 {amount} 金币。\n"
                        f"🎲 成功率：{int(success_rate*100)}%\n"
                        f"💵 当前余额：{user_data['coins']} 金币"
                    )
                else:
                    # 抢劫失败，进监狱
                    penalty = int(user_data.get("coins", 0) * 0.1)
                    user_data["coins"] = max(0, user_data["coins"] - penalty)
                    user_data["jailed_until"] = int(time.time()) + 86400  # 禁言1天
                    self._save_user_data(group_id, user_id, user_data)
                    
                    yield event.plain_result(
                        f"🚨 抢劫失败！{user_name} 被送入监狱！\n"
                        f"💸 扣除 {penalty} 金币作为罚款\n"
                        f"🔒 24小时内无法使用任何指令\n"
                        f"🎲 成功率：{int(success_rate*100)}%\n"
                        f"💵 当前余额：{user_data['coins']} 金币"
                    )

    # ==================== 管理员命令 ====================
    def _is_admin(self, user_id: str) -> bool:
        """检查是否是管理员"""
        admin_list = self.config.get("admin_uins", [])
        # 如果配置为空，使用硬编码的默认管理员
        if not admin_list:
            admin_list = ["846994183", "3864670906"]
        return user_id in admin_list

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
    async def admin_give_coins(self, event: AstrMessageEvent, amount: int):
        """管理员给指定用户发钱"""
        user_id = str(event.get_sender_id())
        if not self._is_admin(user_id):
            yield event.plain_result("❌ 你没有权限使用该指令。")
            return

        target_id = self._extract_target(event)
        if not target_id:
            yield event.plain_result("❌ 请使用@或QQ号指定用户。")
            return

        if amount <= 0 or amount > 100000:
            yield event.plain_result("❌ 一次最多 100000 金币。")
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
