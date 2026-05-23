import json
import os
import random
import time
import re
import asyncio
import hashlib
import shutil
from typing import List, Dict, Any
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.api import logger

class PointsMallPlugin(Star):
    MAX_RECORDS_PER_JSON = 500

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 数据文件统一存放在插件专属的数据目录下，防止升级或容器迁移时丢失
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_points_mall")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_path = os.path.join(self.data_dir, "points_data.json")
        self.points_data = self._load_data()
        if self._cleanup_expired_sold_stock_records() > 0:
            self._save_data()
        self.active_cooldowns = {} # user_id -> last_reward_time (仅保存在内存中)
        self.purchase_lock = asyncio.Lock()

    def _write_json_atomic(self, path: str, data: Any):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        bak_path = f"{path}.bak"
        if os.path.exists(path):
            try:
                shutil.copy2(path, bak_path)
            except Exception as e:
                logger.warning(f"PointsMall 备份数据文件失败 {path}: {e}")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _chunk_path(self, kind: str, index: int) -> str:
        if index <= 1:
            return self.data_path
        return os.path.join(self.data_dir, f"points_data_{kind}_{index}.json")

    def _stock_chunk_path(self, item_id: int, index: int) -> str:
        return os.path.join(self.data_dir, f"points_stock_item_{item_id}_{index}.json")

    def _split_chunks(self, records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        if not records:
            return [[]]
        return [records[i:i + self.MAX_RECORDS_PER_JSON] for i in range(0, len(records), self.MAX_RECORDS_PER_JSON)]

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    def _mask_secret(self, text: str) -> str:
        text = str(text)
        if len(text) <= 8:
            return "*" * len(text)
        return f"{text[:4]}****{text[-4:]}"

    def _valid_order_statuses(self) -> set:
        return {"completed", "refunded"}

    def _sold_stock_retention_days(self) -> int:
        if self.points_data.get("sold_stock_retention_days_manual"):
            value = self.points_data.get("sold_stock_retention_days", 30)
        else:
            value = self.conf.get("sold_stock_retention_days", self.points_data.get("sold_stock_retention_days", 30))
        try:
            return int(value)
        except Exception:
            return 30

    def _sold_stock_delete_after(self, sold_at: int) -> int:
        days = self._sold_stock_retention_days()
        if days < 0:
            return 0
        return int(sold_at) + days * 86400

    def _cleanup_expired_sold_stock_records(self) -> int:
        now = int(time.time())
        cleaned = 0
        for item in self.points_data.get("items", []):
            for stock in item.get("stock", []):
                delete_after = int(stock.get("delete_after", 0) or 0)
                if stock.get("sold") and delete_after > 0 and delete_after <= now and not stock.get("content_deleted"):
                    content = str(stock.get("content", ""))
                    if content and not stock.get("content_hash"):
                        stock["content_hash"] = self._hash_text(content)
                    stock["content"] = ""
                    stock["content_deleted"] = True
                    stock["content_deleted_at"] = now
                    cleaned += 1
        return cleaned

    def _log_points(self, user_id: str, change: int, reason: str, operator: str = "system", order_id: int = 0, item_id: int = 0):
        self.points_data.setdefault("points_logs", [])
        user = self._get_user(user_id)
        self.points_data["points_logs"].append({
            "time": int(time.time()),
            "user_id": self._clean_user_id(user_id),
            "change": int(change),
            "balance": int(user.get("points", 0)),
            "reason": reason,
            "operator": self._clean_user_id(operator) if operator != "system" else "system",
            "order_id": int(order_id or 0),
            "item_id": int(item_id or 0),
        })

    def _load_chunked_records(self, kind: str, base_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not os.path.exists(self._chunk_path(kind, 2)):
            return list(base_records)

        records = list(base_records)
        index = 2
        while True:
            path = self._chunk_path(kind, index)
            if not os.path.exists(path):
                break
            try:
                with open(path, "r", encoding="utf-8") as f:
                    chunk = json.load(f)
                    if isinstance(chunk, list):
                        records.extend(chunk)
            except Exception as e:
                logger.error(f"PointsMall 载入 {kind} 分片 {index} 失败: {e}")
                break
            index += 1
        return records

    def _load_item_stock(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        item_id = int(item.get("id", 0))
        if item_id <= 0 or not os.path.exists(self._stock_chunk_path(item_id, 1)):
            return list(item.get("stock", []))

        stock_records = []
        index = 1
        while True:
            path = self._stock_chunk_path(item_id, index)
            if not os.path.exists(path):
                break
            try:
                with open(path, "r", encoding="utf-8") as f:
                    chunk = json.load(f)
                    if isinstance(chunk, list):
                        stock_records.extend(chunk)
            except Exception as e:
                logger.error(f"PointsMall 载入商品 {item_id} 库存分片 {index} 失败: {e}")
                break
            index += 1
        return stock_records

    def _item_without_stock(self, item: Dict[str, Any]) -> Dict[str, Any]:
        saved_item = dict(item)
        saved_item["stock"] = []
        return saved_item

    def _save_item_stock(self, item: Dict[str, Any]):
        item_id = int(item.get("id", 0))
        if item_id <= 0:
            return

        stock_chunks = self._split_chunks(list(item.get("stock", [])))
        for index, chunk in enumerate(stock_chunks, 1):
            path = self._stock_chunk_path(item_id, index)
            self._write_json_atomic(path, chunk)

        index = len(stock_chunks) + 1
        while True:
            path = self._stock_chunk_path(item_id, index)
            if not os.path.exists(path):
                break
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"PointsMall 删除商品 {item_id} 多余库存分片 {index} 失败: {e}")
                break
            index += 1

    def _save_chunked_records(self, kind: str, chunks: List[List[Dict[str, Any]]]):
        for index, chunk in enumerate(chunks[1:], 2):
            path = self._chunk_path(kind, index)
            self._write_json_atomic(path, chunk)

        index = len(chunks) + 1
        while True:
            path = self._chunk_path(kind, index)
            if not os.path.exists(path):
                break
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"PointsMall 删除多余的 {kind} 分片 {index} 失败: {e}")
                break
            index += 1

    def _load_data(self) -> Dict[str, Any]:
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 兼容旧版本数据结构
                    if "users" not in data:
                        data["users"] = {}
                    if "items" not in data:
                        data["items"] = []
                    if "item_counter" not in data:
                        data["item_counter"] = 0
                    if "orders" not in data:
                        data["orders"] = []
                    if "order_counter" not in data:
                        data["order_counter"] = 0
                    if "points_logs" not in data:
                        data["points_logs"] = []
                    if "sold_stock_retention_days" not in data:
                        data["sold_stock_retention_days"] = self.conf.get("sold_stock_retention_days", 30)
                    data["items"] = self._load_chunked_records("items", data.get("items", []))
                    data["orders"] = self._load_chunked_records("orders", data.get("orders", []))
                    for item in data["items"]:
                        item.setdefault("delivery_mode", "auto")
                        item.setdefault("delivery_type", "stock")
                        item.setdefault("delivery_content", "")
                        item["stock"] = self._load_item_stock(item)
                        item.setdefault("stock_counter", len(item.get("stock", [])))
                    return data
            except Exception as e:
                logger.error(f"PointsMall 载入数据失败: {e}")
        return {
            "users": {},     # {user_id: {"points": 0, "last_sign": "YYYYMMDD"}}
            "items": [],     # 商品列表。超过 500 条后会自动拆分到 points_data_items_2.json 等文件
            "item_counter": 0,
            "orders": [],    # 订单列表。超过 500 条后会自动拆分到 points_data_orders_2.json 等文件
            "order_counter": 0,
            "points_logs": [], # 积分流水
            "sold_stock_retention_days": self.conf.get("sold_stock_retention_days", 30) # 已售库存内容保留天数，-1 表示不自动删除
        }

    def _save_data(self):
        try:
            self._cleanup_expired_sold_stock_records()
            base_data = dict(self.points_data)
            items = list(base_data.get("items", []))
            orders = list(base_data.get("orders", []))
            saved_items = [self._item_without_stock(item) for item in items]
            item_chunks = self._split_chunks(saved_items)
            order_chunks = self._split_chunks(orders)
            base_data["items"] = item_chunks[0]
            base_data["orders"] = order_chunks[0]
            self._write_json_atomic(self.data_path, base_data)
            self._save_chunked_records("items", item_chunks)
            self._save_chunked_records("orders", order_chunks)
            for item in items:
                self._save_item_stock(item)
        except Exception as e:
            logger.error(f"PointsMall 保存数据失败: {e}")

    def _get_user(self, user_id: str):
        # 统一过滤提取纯数字ID（适配QQ等平台）
        clean_uid = "".join(re.findall(r"\d+", str(user_id)))
        if not clean_uid:
            clean_uid = str(user_id)
        if clean_uid not in self.points_data["users"]:
            self.points_data["users"][clean_uid] = {"points": 0, "last_sign": ""}
        return self.points_data["users"][clean_uid]

    def _is_super_admin(self, user_id: str) -> bool:
        clean_uid = "".join(re.findall(r"\d+", str(user_id)))
        super_admins = [str(x) for x in self.conf.get("super_admins", [])]
        return clean_uid in super_admins

    def _is_admin(self, user_id: str) -> bool:
        clean_uid = "".join(re.findall(r"\d+", str(user_id)))
        super_admins = [str(x) for x in self.conf.get("super_admins", [])]
        admins = [str(x) for x in self.conf.get("admins", [])]
        return clean_uid in super_admins or clean_uid in admins

    def _clean_user_id(self, user_id: str) -> str:
        clean_uid = "".join(re.findall(r"\d+", str(user_id)))
        return clean_uid or str(user_id)

    def _format_time(self, timestamp: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))

    def _use_t2i(self) -> bool:
        return bool(self.conf.get("use_t2i", True))

    def _plain_result(self, event: AstrMessageEvent, text: str):
        return event.plain_result(text).use_t2i(self._use_t2i())

    def _private_plain_result(self, event: AstrMessageEvent, text: str):
        return event.plain_result(text).use_t2i(False)

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        try:
            return event.get_message_type() == MessageType.FRIEND_MESSAGE
        except Exception:
            return not bool(event.get_group_id())

    def _create_private_session(self, event: AstrMessageEvent, user_id: str) -> MessageSession:
        return MessageSession(
            platform_name=event.get_platform_id(),
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=self._clean_user_id(user_id),
        )

    def _find_item(self, item_id: int):
        for item in self.points_data["items"]:
            if item.get("id") == item_id:
                item.setdefault("delivery_mode", "auto")
                item.setdefault("delivery_type", "stock")
                item.setdefault("delivery_content", "")
                item.setdefault("delivery_version", 0)
                item.setdefault("delivery_content_hash", "")
                item.setdefault("stock", [])
                item.setdefault("stock_counter", len(item.get("stock", [])))
                return item
        return None

    def _can_manage_item(self, user_id: str, item: Dict[str, Any]) -> bool:
        clean_uid = self._clean_user_id(user_id)
        return self._is_super_admin(user_id) or (self._is_admin(user_id) and item.get("creator") == clean_uid)

    def _available_stock(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [stock for stock in item.get("stock", []) if not stock.get("sold")]

    def _take_stock(self, item: Dict[str, Any], user_id: str, order_id: int):
        for stock in item.get("stock", []):
            if not stock.get("sold"):
                stock["sold"] = True
                stock["buyer"] = user_id
                stock["order_id"] = order_id
                stock["sold_at"] = int(time.time())
                stock["delete_after"] = self._sold_stock_delete_after(stock["sold_at"])
                return stock
        return None

    def _find_order(self, order_id: int):
        for order in self.points_data.get("orders", []):
            if int(order.get("order_id", 0)) == int(order_id):
                return order
        return None

    async def _send_private_notice(self, event: AstrMessageEvent, user_id: str, message: str) -> bool:
        try:
            session = self._create_private_session(event, user_id)
            chain = MessageChain([Plain(message)]).use_t2i(self._use_t2i())
            return await self.context.send_message(session, chain)
        except Exception as e:
            logger.warning(f"PointsMall 私聊通知 {user_id} 失败: {e}")
            return False

    async def _notify_item_approval_admins(self, event: AstrMessageEvent, item: Dict[str, Any]) -> bool:
        notify_users = []
        for uid in self.conf.get("super_admins", []):
            clean_uid = self._clean_user_id(str(uid))
            if clean_uid and clean_uid not in notify_users:
                notify_users.append(clean_uid)

        if not notify_users:
            return False

        notice = (
            "📦 积分商城有新的商品上架申请\n"
            f"申请人：QQ {item['creator']}\n"
            f"商品序号：{item['id']}\n"
            f"商品名称：{item['name']}\n"
            f"商品价格：{item['price']} 积分\n"
            f"商品描述：{item.get('desc') or '无'}\n"
            "\n审核通过：授权上架 [序号]\n"
            "拒绝/删除：下架 [序号]"
        )

        sent = False
        for uid in notify_users:
            if await self._send_private_notice(event, uid, notice):
                sent = True
        return sent

    async def _notify_low_stock_admins(self, event: AstrMessageEvent, item: Dict[str, Any], remaining: int) -> bool:
        threshold = max(0, int(self.conf.get("low_stock_warn_threshold", 3)))
        if remaining > threshold:
            item["last_low_stock_warn_remaining"] = None
            item["last_low_stock_warn_at"] = 0
            return False
        if item.get("last_low_stock_warn_remaining") == remaining:
            return False

        notify_users = []
        for uid in self.conf.get("super_admins", []):
            clean_uid = self._clean_user_id(str(uid))
            if clean_uid and clean_uid not in notify_users:
                notify_users.append(clean_uid)
        creator = self._clean_user_id(str(item.get("creator", "")))
        if creator and creator not in notify_users:
            notify_users.append(creator)

        if not notify_users:
            return False

        notice = (
            "⚠️ 积分商城库存不足提醒\n"
            f"商品：[{item['id']}] {item['name']}\n"
            f"当前可用库存：{remaining} 条\n"
            f"提醒阈值：{threshold} 条\n"
            "请尽快补货"
        )

        sent = False
        for uid in notify_users:
            if await self._send_private_notice(event, uid, notice):
                sent = True
        if sent:
            item["last_low_stock_warn_remaining"] = remaining
            item["last_low_stock_warn_at"] = int(time.time())
        return sent

    async def _notify_auto_delivery_admins(self, event: AstrMessageEvent, order: Dict[str, Any]) -> bool:
        notify_users = []
        for uid in self.conf.get("super_admins", []):
            clean_uid = self._clean_user_id(str(uid))
            if clean_uid and clean_uid not in notify_users:
                notify_users.append(clean_uid)
        creator = self._clean_user_id(str(order.get("creator", "")))
        if creator and creator not in notify_users:
            notify_users.append(creator)

        if not notify_users:
            return False

        notice = (
            "✅ 积分商城订单已自动发货\n"
            f"订单号：{order['order_id']}\n"
            f"购买人：QQ {order['user_id']}\n"
            f"商品：[{order['item_id']}] {order['item_name']}\n"
            f"消耗积分：{order['price']}\n"
            f"发货类型：{order.get('delivery_type', 'unknown')}\n"
            f"下单时间：{self._format_time(order['created_at'])}"
        )

        sent = False
        for uid in notify_users:
            if await self._send_private_notice(event, uid, notice):
                sent = True
        return sent

    @filter.command("积分帮助")
    async def points_help(self, event: AstrMessageEvent):
        msg = (
            "📖 积分商场指令帮助\n"
            "\n━━━━━━━━━━━━━━\n"
            "\n👤 用户指令\n"
            "\n/签到\n"
            "\n - 每日签到获取随机积分\n"
            "\n/我的积分\n"
            "\n - 查询自己的积分\n"
            "\n/积分排行\n"
            "\n - 查看积分排行榜前 10 名\n"
            "\n/积分商场\n"
            "\n - 查看已上架商品和自动发货库存\n"
            "\n/购买 [序号]\n"
            "\n - 私聊购买并自动发货，例如：购买 1\n"
            "\n━━━━━━━━━━━━━━\n"
            "\n🛠 管理员指令\n"
            "\n/上架 [名称] [价格] [描述]\n"
            "\n - 上架/申请上架商品\n"
            "\n/设置发货 [序号] [固定发货内容]\n"
            "\n - 设置无限次自动发货内容\n"
            "\n/添加库存 [序号] [卡密/兑换码]\n"
            "\n - 添加一次性自动发货库存\n"
            "\n/批量添加库存 [序号] [多行库存或 | 分隔库存]\n"
            "\n - 一次添加多条一次性库存\n"
            "\n/查看库存 [序号]\n"
            "\n - 查看商品库存状态\n"
            "\n/清空库存 [序号]\n"
            "\n - 清空未售出库存，保留已售记录\n"
            "\n/待审核商品\n"
            "\n - 查看待审核商品\n"
            "\n/下架 [序号]\n"
            "\n - 下架商品\n"
            "\n/订单列表 [已完成/已退款/全部]\n"
            "\n - 查看订单\n"
            "\n/查订单 [订单号]\n"
            "\n - 查看单个订单详情\n"
            "\n/库存明细 [序号]\n"
            "\n - 私聊查看库存脱敏明细\n"
            "\n/导出库存 [序号]\n"
            "\n - 私聊导出未售库存\n"
            "\n/清理已售库存 [序号] [天数]\n"
            "\n - 手动强制清理指定天数前的已售库存记录\n"
            "\n━━━━━━━━━━━━━━\n"
            "\n👑 超级管理员指令\n"
            "\n/授权上架 [序号]\n"
            "\n - 审核通过商品\n"
            "\n/给予积分 [QQ号/@用户] [积分数量]\n"
            "\n - 增加或扣除积分\n"
            "\n/强制退款 [订单号]\n"
            "\n - 强制退回订单积分\n"
            "\n/商城自检\n"
            "\n - 检查商城数据健康状态\n"
            "\n/积分流水 [QQ号]\n"
            "\n - 查看指定用户最近积分变动\n"
            "\n/设置已售保留天数 [天数]\n"
            "\n - 设置已售库存内容进入删除流程后的保留时间，-1 表示不自动删除\n"
            "\n/清理到期已售库存\n"
            "\n - 按删除流程清理已到期的已售库存真实内容\n"
            "\n━━━━━━━━━━━━━━\n"
            "\n📝 示例\n"
            "\n/上架 月卡 100 兑换一张月卡\n"
            "\n/设置发货 1 网盘链接：https://example.com\n"
            "\n/添加库存 1 ABCD-EFGH-IJKL\n"
            "\n/批量添加库存 1 A卡密|B卡密|C卡密\n"
            "\n/给予积分 123456 100\n"
            "\n/给予积分 123456 -50"
        )
        yield self._plain_result(event, msg)

    @filter.on_decorating_result()
    async def on_any_message(self, event: AstrMessageEvent):
        # 只在群聊消息中积攒活跃积分，私聊不计算
        if self._is_private_event(event):
            return
            
        user_id = event.get_sender_id()
        if not user_id:
            return
            
        text = event.get_message_str().strip()
        
        # 过滤掉指令、签到、购买等消息，防止刷屏指令获取积分
        if text.startswith(("/", "!", "！", "购买", "签到", "我的积分", "积分商场", "积分排行", "给予积分", "积分帮助", "上架", "待审核商品", "下架", "授权上架", "订单列表", "查订单", "强制退款", "设置发货", "添加库存", "批量添加库存", "查看库存", "库存明细", "导出库存", "清理已售库存", "设置已售保留天数", "清理到期已售库存", "清空库存", "商城自检", "积分流水")):
            return
            
        # 字数判定：严格超过配置阈值才算有效活跃，例如默认 10 表示至少 11 个文字
        if len(text) > self.conf.get("active_text_len", 10):
            now = time.time()
            last_time = self.active_cooldowns.get(user_id, 0)
            if now - last_time >= self.conf.get("active_cooldown", 60):
                user = self._get_user(user_id)
                user["points"] += self.conf.get("active_point_reward", 1)
                self._log_points(user_id, self.conf.get("active_point_reward", 1), "active_reward")
                self.active_cooldowns[user_id] = now
                self._save_data()

    @filter.command("签到")
    async def sign_in(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        user = self._get_user(user_id)
        today = time.strftime("%Y%m%d")
        
        if user["last_sign"] == today:
            yield self._plain_result(event, "主人，你今天已经签到过了哦~ 不要太贪心嘛。")
            return
        
        gain = random.randint(self.conf.get("sign_min", 10), self.conf.get("sign_max", 50))
        user["points"] += gain
        user["last_sign"] = today
        self._log_points(user_id, gain, "sign_in")
        self._save_data()
        yield self._plain_result(event, f"签到成功！获得 {gain} 积分。当前总积分：{user['points']}。")

    @filter.command("我的积分")
    async def my_points(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        user = self._get_user(user_id)
        yield self._plain_result(event, f"主人，你目前的积分为：{user['points']} 点。")

    @filter.command("积分排行")
    async def points_leaderboard(self, event: AstrMessageEvent):
        users = self.points_data.get("users", {})
        if not users:
            yield self._plain_result(event, "目前还没有任何人有积分记录哦~")
            return
            
        # 按照积分从高到低排序
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        top_10 = sorted_users[:10]
        
        msg = "🏆 === 积分龙虎榜 === 🏆\n"
        for idx, (uid, info) in enumerate(top_10, 1):
            msg += f"第 {idx} 名: QQ {uid} | {info.get('points', 0)} 积分\n"
        yield self._plain_result(event, msg.strip())

    @filter.command("上架")
    async def add_item(self, event: AstrMessageEvent, name: str, price: int, description: str = ""):
        user_id = event.get_sender_id()
        if not self._is_admin(user_id):
            yield self._plain_result(event, "只有管理员才能上架商品哦！")
            return
        if price <= 0:
            yield self._plain_result(event, "商品价格必须大于 0 积分。")
            return
        
        self.points_data["item_counter"] += 1
        new_id = self.points_data["item_counter"]
        
        # 超级管理员直接上架，普通管理员需要审核
        status = "approved" if self._is_super_admin(user_id) else "pending"
        
        item = {
            "id": new_id,
            "name": name,
            "price": price,
            "desc": description,
            "status": status,
            "creator": "".join(re.findall(r"\d+", str(user_id))),
            "delivery_mode": "auto",
            "delivery_type": "stock",
            "delivery_content": "",
            "stock": [],
            "stock_counter": 0
        }
        self.points_data["items"].append(item)
        self._save_data()
        
        if status == "approved":
            yield self._plain_result(event, f"成功上架商品！[序号 {new_id}] {name} - {price} 积分。请继续使用 设置发货 或 添加库存 配置自动发货内容。")
        else:
            notified = await self._notify_item_approval_admins(event, item)
            notify_text = "已私聊通知超级管理员审核。" if notified else "未能自动通知超级管理员，请主动联系超级管理员审核。"
            yield self._plain_result(event, f"商品「{name}」已提交申请（序号 {new_id}），请等待超级管理员授权上架。{notify_text} 审核通过后请使用 设置发货 或 添加库存 配置自动发货内容。")

    @filter.command("待审核商品")
    async def pending_list(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        if not self._is_admin(user_id):
            yield self._plain_result(event, "只有管理员才能查看待审核商品列表哦！")
            return
            
        pending_items = [i for i in self.points_data["items"] if i["status"] == "pending"]
        if not pending_items:
            yield self._plain_result(event, "当前没有待审核的商品~")
            return
            
        msg = "📋 === 待审核商品列表 === 📋\n"
        for item in pending_items:
            msg += f"序号 {item['id']}. {item['name']} | {item['price']} 积分\n   描述: {item['desc']}\n   申请人: QQ {item['creator']}\n"
        msg += "\n* 超级管理员可发送 '授权上架 [序号]' 或 '下架 [序号]'"
        yield self._plain_result(event, msg.strip())

    @filter.command("授权上架")
    async def approve_item(self, event: AstrMessageEvent, item_id: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "哼，这个权限只有超级管理员才有哦！")
            return
        
        for item in self.points_data["items"]:
            if item["id"] == item_id:
                if item["status"] == "approved":
                    yield self._plain_result(event, f"商品 [序号 {item_id}] 已经是上架状态啦。")
                    return
                item["status"] = "approved"
                self._save_data()
                yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」已授权通过，正式开售！")
                return
        yield self._plain_result(event, f"没找到序号为 {item_id} 的商品。")

    @filter.command("下架")
    async def remove_item(self, event: AstrMessageEvent, item_id: int):
        user_id = event.get_sender_id()
        clean_uid = "".join(re.findall(r"\d+", str(user_id)))
        
        target_item = None
        for item in self.points_data["items"]:
            if item["id"] == item_id:
                target_item = item
                break
                
        if not target_item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
            
        # 只有超级管理员，或者该商品的创建者（且为管理员）可以下架
        if self._is_super_admin(user_id) or (target_item["creator"] == clean_uid and self._is_admin(user_id)):
            target_item["status"] = "removed"
            target_item["removed_at"] = int(time.time())
            target_item["removed_by"] = clean_uid
            self._save_data()
            yield self._plain_result(event, f"商品 [序号 {item_id}]「{target_item['name']}」已成功下架，历史订单仍会保留关联记录。")
        else:
            yield self._plain_result(event, "哼，你没有权限下架这个商品哦！")

    @filter.command("设置发货")
    async def set_fixed_delivery(self, event: AstrMessageEvent, item_id: int, content: str):
        if not self._is_private_event(event):
            yield self._plain_result(event, "发货内容包含真实商品数据，请私聊我使用 设置发货。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以设置该商品的发货内容。")
            return
        item["delivery_mode"] = "auto"
        item["delivery_type"] = "fixed"
        item["delivery_content"] = content
        item["delivery_version"] = int(item.get("delivery_version", 0)) + 1
        item["delivery_content_hash"] = self._hash_text(content)
        self._save_data()
        yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」已设置为固定内容自动发货，当前发货版本：v{item['delivery_version']}。")

    @filter.command("添加库存")
    async def add_stock(self, event: AstrMessageEvent, item_id: int, content: str):
        if not self._is_private_event(event):
            yield self._plain_result(event, "库存内容包含真实商品数据，请私聊我使用 添加库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以添加该商品库存。")
            return
        item["delivery_mode"] = "auto"
        item["delivery_type"] = "stock"
        item.setdefault("stock", [])
        if content in {str(stock.get("content", "")) for stock in item["stock"]}:
            yield self._plain_result(event, f"该库存内容已存在，未重复添加。当前可用库存：{len(self._available_stock(item))}。")
            return
        item["stock_counter"] = int(item.get("stock_counter", len(item.get("stock", [])))) + 1
        item["stock"].append({
            "id": item["stock_counter"],
            "content": content,
            "sold": False,
            "buyer": "",
            "order_id": 0,
            "sold_at": 0
        })
        self._save_data()
        yield self._plain_result(event, f"已为商品 [序号 {item_id}]「{item['name']}」添加 1 条库存。当前可用库存：{len(self._available_stock(item))}。")

    @filter.command("批量添加库存")
    async def batch_add_stock(self, event: AstrMessageEvent, item_id: int, contents: str = ""):
        if not self._is_private_event(event):
            yield self._plain_result(event, "库存内容包含真实商品数据，请私聊我使用 批量添加库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以添加该商品库存。")
            return

        raw_text = event.get_message_str().strip()
        match = re.match(r"^[!/！/]?批量添加库存\s+\d+\s*(.*)$", raw_text, re.S)
        payload = match.group(1).strip() if match else contents.strip()
        if not payload:
            yield self._plain_result(event, "请输入要添加的库存内容。支持换行或 | 分隔，例如：批量添加库存 1 A卡密|B卡密|C卡密")
            return

        candidates = []
        seen = set()
        for line in re.split(r"[\n|]+", payload.replace("\r", "\n")):
            content = line.strip()
            if content and content not in seen:
                candidates.append(content)
                seen.add(content)

        if not candidates:
            yield self._plain_result(event, "没有解析到有效库存内容。")
            return

        item["delivery_mode"] = "auto"
        item["delivery_type"] = "stock"
        item.setdefault("stock", [])
        existing_contents = {str(stock.get("content", "")) for stock in item["stock"]}
        added = 0
        skipped_existing = 0
        for content in candidates:
            if content in existing_contents:
                skipped_existing += 1
                continue
            item["stock_counter"] = int(item.get("stock_counter", len(item.get("stock", [])))) + 1
            item["stock"].append({
                "id": item["stock_counter"],
                "content": content,
                "sold": False,
                "buyer": "",
                "order_id": 0,
                "sold_at": 0
            })
            existing_contents.add(content)
            added += 1

        self._save_data()
        duplicate_input = len(re.split(r"[\n|]+", payload.replace("\r", "\n"))) - len(candidates)
        yield self._plain_result(event, (
            f"批量添加库存完成。\n"
            f"商品：[序号 {item_id}]「{item['name']}」\n"
            f"新增：{added} 条\n"
            f"跳过已有：{skipped_existing} 条\n"
            f"输入内重复/空行：{duplicate_input} 条\n"
            f"当前可用库存：{len(self._available_stock(item))} 条"
        ))

    @filter.command("查看库存")
    async def view_stock(self, event: AstrMessageEvent, item_id: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "库存状态建议私聊查看，请私聊我使用 查看库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以查看该商品库存。")
            return
        if item.get("delivery_type") == "fixed":
            configured = "已配置" if item.get("delivery_content") else "未配置"
            yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」为固定内容自动发货，状态：{configured}。")
            return
        stock = item.get("stock", [])
        available = len(self._available_stock(item))
        sold = len([s for s in stock if s.get("sold")])
        yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」库存状态：可用 {available} 条，已售 {sold} 条，总计 {len(stock)} 条。")

    @filter.command("库存明细")
    async def stock_detail(self, event: AstrMessageEvent, item_id: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "库存明细包含敏感信息，请私聊我使用 库存明细。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以查看该商品库存明细。")
            return
        if item.get("delivery_type") == "fixed":
            yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」是固定内容发货，无一次性库存明细。")
            return
        stock = item.get("stock", [])
        available = [s for s in stock if not s.get("sold")]
        sold = [s for s in stock if s.get("sold")]
        msg = (
            f"📦 商品 [序号 {item_id}]「{item['name']}」库存明细\n"
            f"可用：{len(available)} 条，已售：{len(sold)} 条，总计：{len(stock)} 条\n\n"
            "可用库存前 10 条：\n"
        )
        for stock_item in available[:10]:
            msg += f"#{stock_item.get('id')} {self._mask_secret(stock_item.get('content', ''))}\n"
        if not available:
            msg += "无\n"
        msg += "\n已售库存前 10 条：\n"
        for stock_item in sold[:10]:
            sold_at = self._format_time(stock_item.get("sold_at", 0)) if stock_item.get("sold_at") else "未知"
            delete_after = int(stock_item.get("delete_after", 0) or 0)
            if stock_item.get("content_deleted"):
                content_text = "[内容已按规则删除]"
                delete_text = self._format_time(stock_item.get("content_deleted_at", 0)) if stock_item.get("content_deleted_at") else "已删除"
            else:
                content_text = self._mask_secret(stock_item.get("content", ""))
                delete_text = self._format_time(delete_after) if delete_after > 0 else "不自动删除"
            msg += f"#{stock_item.get('id')} {content_text} | 订单 {stock_item.get('order_id', 0)} | QQ {stock_item.get('buyer', '')} | 售出 {sold_at} | 删除时间 {delete_text}\n"
        if not sold:
            msg += "无"
        yield self._private_plain_result(event, msg.strip())

    @filter.command("导出库存")
    async def export_stock(self, event: AstrMessageEvent, item_id: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "导出库存包含真实商品数据，请私聊我使用 导出库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以导出该商品库存。")
            return
        if item.get("delivery_type") == "fixed":
            yield self._private_plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」固定发货内容：\n{item.get('delivery_content', '')}")
            return
        available = self._available_stock(item)
        if not available:
            yield self._plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」当前没有可导出的未售库存。")
            return
        lines = [str(stock.get("content", "")) for stock in available]
        yield self._private_plain_result(event, f"商品 [序号 {item_id}]「{item['name']}」未售库存导出，共 {len(lines)} 条：\n" + "\n".join(lines))

    @filter.command("清理已售库存")
    async def cleanup_sold_stock(self, event: AstrMessageEvent, item_id: int, days: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "清理库存属于敏感操作，请私聊我使用 清理已售库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以清理该商品库存。")
            return
        if days < 0:
            yield self._plain_result(event, "天数不能小于 0。")
            return
        cutoff = int(time.time()) - days * 86400
        before = len(item.get("stock", []))
        item["stock"] = [stock for stock in item.get("stock", []) if not stock.get("sold") or int(stock.get("sold_at", 0)) >= cutoff]
        removed = before - len(item["stock"])
        self._save_data()
        yield self._plain_result(event, f"已清理商品 [序号 {item_id}]「{item['name']}」{days} 天前的已售库存记录 {removed} 条，订单记录仍保留。")

    @filter.command("设置已售保留天数")
    async def set_sold_stock_retention_days(self, event: AstrMessageEvent, days: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有超级管理员才能设置已售库存保留天数。")
            return
        if days < -1:
            yield self._plain_result(event, "保留天数不能小于 -1。-1 表示不自动删除。")
            return
        self.points_data["sold_stock_retention_days"] = days
        self.points_data["sold_stock_retention_days_manual"] = True
        now = int(time.time())
        updated = 0
        for item in self.points_data.get("items", []):
            for stock in item.get("stock", []):
                if stock.get("sold"):
                    sold_at = int(stock.get("sold_at", now) or now)
                    stock["delete_after"] = 0 if days < 0 else sold_at + days * 86400
                    updated += 1
        self._save_data()
        if days < 0:
            yield self._plain_result(event, f"已设置已售库存内容不自动删除，并更新 {updated} 条已售记录。")
        else:
            yield self._plain_result(event, f"已设置已售库存内容售出后保留 {days} 天，到期进入删除流程，并更新 {updated} 条已售记录。部分历史已售库存可能已到期，可执行 清理到期已售库存。")

    @filter.command("清理到期已售库存")
    async def cleanup_expired_sold_stock(self, event: AstrMessageEvent):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有超级管理员才能清理到期已售库存。")
            return
        cleaned = self._cleanup_expired_sold_stock_records()
        self._save_data()
        yield self._plain_result(event, f"已清理到期已售库存真实内容 {cleaned} 条，库存元数据和订单记录仍保留。")

    @filter.command("清空库存")
    async def clear_stock(self, event: AstrMessageEvent, item_id: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "清空库存属于敏感操作，请私聊我使用 清空库存。")
            return
        user_id = event.get_sender_id()
        item = self._find_item(item_id)
        if not item:
            yield self._plain_result(event, f"没有找到序号为 {item_id} 的商品。")
            return
        if not self._can_manage_item(user_id, item):
            yield self._plain_result(event, "只有超级管理员或商品上架者可以清空该商品库存。")
            return
        before = len(item.get("stock", []))
        item["stock"] = [stock for stock in item.get("stock", []) if stock.get("sold")]
        removed = before - len(item["stock"])
        self._save_data()
        yield self._plain_result(event, f"已清空商品 [序号 {item_id}]「{item['name']}」的未售出库存 {removed} 条，已售记录保留。")

    @filter.command("积分商场")
    async def mall_list(self, event: AstrMessageEvent):
        msg = "=== 积分商场 ===\n"
        approved_items = [i for i in self.points_data["items"] if i["status"] == "approved"]
        if not approved_items:
            msg += "目前商场空空如也呢..."
        else:
            for item in approved_items:
                if item.get("delivery_type") == "fixed":
                    stock_text = "自动发货：固定内容"
                else:
                    stock_text = f"自动发货库存：{len(self._available_stock(item))}"
                msg += f"{item['id']}. {item['name']} | {item['price']} 积分\n   描述: {item['desc']}\n   {stock_text}\n"
        msg += "\n* 请私聊我发送 '购买 [序号]' 进行购买~"
        yield self._plain_result(event, msg.strip())

    @filter.command("给予积分")
    async def give_points(self, event: AstrMessageEvent, target_qq: str, amount: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "权限不足。")
            return
            
        # 提取 target_qq 中的所有数字（适配@或者纯QQ）
        clean_qq = "".join(re.findall(r"\d+", target_qq))
        if not clean_qq:
            yield self._plain_result(event, "请输入有效的QQ号或艾特目标成员哦！")
            return
            
        user = self._get_user(clean_qq)
        user["points"] += amount
        self._log_points(clean_qq, amount, "admin_adjust", event.get_sender_id())
        self._save_data()
        
        action = "给予" if amount >= 0 else "扣除"
        abs_amount = abs(amount)
        yield self._plain_result(event, f"成功为 QQ {clean_qq} {action} {abs_amount} 积分。当前总积分：{user['points']}。")

    @filter.command("订单列表")
    async def order_list(self, event: AstrMessageEvent, status: str = "all"):
        if not self._is_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有管理员才能查看订单列表哦！")
            return

        status_map = {
            "已完成": "completed",
            "完成": "completed",
            "已退款": "refunded",
            "退款": "refunded",
            "全部": "all",
            "all": "all",
            "completed": "completed",
            "refunded": "refunded",
        }
        query_status = status_map.get(status, status)
        orders = self.points_data.get("orders", [])
        if query_status != "all":
            orders = [o for o in orders if o.get("status") == query_status]
        orders = sorted(orders, key=lambda x: x.get("order_id", 0), reverse=True)[:10]

        if not orders:
            yield self._plain_result(event, "当前没有符合条件的订单。")
            return

        msg = "📦 === 积分商城订单列表 === 📦\n"
        for order in orders:
            msg += (
                f"订单 {order['order_id']} | {order.get('status', 'unknown')}\n"
                f"购买人: QQ {order['user_id']}\n"
                f"商品: [{order['item_id']}] {order['item_name']} | {order['price']} 积分\n"
                f"时间: {self._format_time(order['created_at'])}\n"
            )
        msg += "\n可用命令：强制退款 [订单号]"
        yield self._plain_result(event, msg.strip())

    @filter.command("查订单")
    async def query_order(self, event: AstrMessageEvent, order_id: int):
        if not self._is_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有管理员才能查看订单详情。")
            return
        order = self._find_order(order_id)
        if not order:
            yield self._plain_result(event, f"没有找到订单号为 {order_id} 的订单。")
            return
        item = self._find_item(int(order.get("item_id", 0)))
        stock_text = "无"
        if item and order.get("delivery_stock_id"):
            for stock in item.get("stock", []):
                if int(stock.get("id", 0)) == int(order.get("delivery_stock_id", 0)):
                    stock_text = "[内容已按规则删除]" if stock.get("content_deleted") else self._mask_secret(stock.get("content", ""))
                    break
        msg = (
            "📦 积分商城订单详情\n"
            f"订单号：{order.get('order_id')}\n"
            f"状态：{order.get('status', 'unknown')}\n"
            f"购买人：QQ {order.get('user_id')}\n"
            f"商品：[{order.get('item_id')}] {order.get('item_name')}\n"
            f"价格：{order.get('price')} 积分\n"
            f"上架人：QQ {order.get('creator', '')}\n"
            f"下单时间：{self._format_time(order.get('created_at', 0))}\n"
            f"完成时间：{self._format_time(order.get('completed_at', 0)) if order.get('completed_at') else '无'}\n"
            f"退款时间：{self._format_time(order.get('refunded_at', 0)) if order.get('refunded_at') else '无'}\n"
            f"发货类型：{order.get('delivery_type', 'unknown')}\n"
            f"库存 ID：{order.get('delivery_stock_id', 0)}\n"
            f"发货版本：v{order.get('delivery_version', 0)}\n"
            f"发货哈希：{order.get('delivery_hash', '')}\n"
            f"库存内容脱敏：{stock_text}"
        )
        yield self._private_plain_result(event, msg)

    @filter.command("积分流水")
    async def points_logs(self, event: AstrMessageEvent, target_qq: str = ""):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有超级管理员才能查看积分流水。")
            return
        clean_qq = self._clean_user_id(target_qq or event.get_sender_id())
        logs = [log for log in self.points_data.get("points_logs", []) if log.get("user_id") == clean_qq]
        logs = sorted(logs, key=lambda x: x.get("time", 0), reverse=True)[:10]
        if not logs:
            yield self._plain_result(event, f"QQ {clean_qq} 暂无积分流水。")
            return
        msg = f"📒 QQ {clean_qq} 最近积分流水\n"
        for log in logs:
            change = int(log.get("change", 0))
            sign = "+" if change >= 0 else ""
            msg += f"{self._format_time(log.get('time', 0))} | {sign}{change} | 余额 {log.get('balance', 0)} | {log.get('reason', '')} | 订单 {log.get('order_id', 0)}\n"
        yield self._plain_result(event, msg.strip())

    @filter.command("商城自检")
    async def mall_self_check(self, event: AstrMessageEvent):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有超级管理员才能执行商城自检。")
            return
        issues = []
        try:
            self._load_data()
        except Exception as e:
            issues.append(f"主数据读取异常：{e}")
        items = self.points_data.get("items", [])
        orders = self.points_data.get("orders", [])
        item_ids = [item.get("id") for item in items]
        order_ids = [order.get("order_id") for order in orders]
        if len(item_ids) != len(set(item_ids)):
            issues.append("存在重复商品 ID")
        if len(order_ids) != len(set(order_ids)):
            issues.append("存在重复订单 ID")
        item_id_set = set(item_ids)
        for order in orders:
            if order.get("item_id") not in item_id_set:
                issues.append(f"订单 {order.get('order_id')} 关联商品不存在")
            if order.get("status") not in self._valid_order_statuses():
                issues.append(f"订单 {order.get('order_id')} 状态异常：{order.get('status')}")
        for item in items:
            stock_ids = [stock.get("id") for stock in item.get("stock", [])]
            if len(stock_ids) != len(set(stock_ids)):
                issues.append(f"商品 {item.get('id')} 存在重复库存 ID")
            for stock in item.get("stock", []):
                if stock.get("sold") and not stock.get("order_id"):
                    issues.append(f"商品 {item.get('id')} 库存 {stock.get('id')} 已售但缺少订单号")
        approved = len([item for item in items if item.get("status") == "approved"])
        pending = len([item for item in items if item.get("status") == "pending"])
        removed = len([item for item in items if item.get("status") == "removed"])
        stock_total = sum(len(item.get("stock", [])) for item in items)
        stock_available = sum(len(self._available_stock(item)) for item in items)
        now = int(time.time())
        stock_pending_delete = sum(1 for item in items for stock in item.get("stock", []) if stock.get("sold") and int(stock.get("delete_after", 0) or 0) > now)
        stock_expired_delete = sum(1 for item in items for stock in item.get("stock", []) if stock.get("sold") and 0 < int(stock.get("delete_after", 0) or 0) <= now)
        stock_content_deleted = sum(1 for item in items for stock in item.get("stock", []) if stock.get("sold") and stock.get("content_deleted"))
        msg = (
            "🧪 积分商城自检完成\n"
            f"商品：总计 {len(items)}，上架 {approved}，待审核 {pending}，已下架 {removed}\n"
            f"订单：{len(orders)} 条\n"
            f"库存：总计 {stock_total} 条，可用 {stock_available} 条\n"
            f"已售库存删除流程：待到期 {stock_pending_delete} 条，已到期 {stock_expired_delete} 条，已删除内容 {stock_content_deleted} 条，保留天数 {self._sold_stock_retention_days()}\n"
            f"积分流水：{len(self.points_data.get('points_logs', []))} 条\n"
            f"检查结果：{'正常' if not issues else '发现问题'}"
        )
        if issues:
            msg += "\n\n问题前 20 条：\n" + "\n".join(f"- {issue}" for issue in issues[:20])
        yield self._plain_result(event, msg)

    @filter.command("强制退款")
    async def force_refund(self, event: AstrMessageEvent, order_id: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield self._plain_result(event, "只有超级管理员才能强制退款。")
            return

        clean_uid = self._clean_user_id(event.get_sender_id())
        for order in self.points_data.get("orders", []):
            if order.get("order_id") == order_id:
                if order.get("status") == "refunded":
                    yield self._plain_result(event, f"订单 {order_id} 已经退款过了，不能重复退款。")
                    return
                if order.get("status") != "completed":
                    yield self._plain_result(event, f"订单 {order_id} 当前状态为 {order.get('status')}，只能强制退款已完成订单。")
                    return

                buyer = self._get_user(order["user_id"])
                buyer["points"] += int(order.get("price", 0))
                self._log_points(order["user_id"], int(order.get("price", 0)), "force_refund", clean_uid, order_id, order.get("item_id", 0))
                order["status"] = "refunded"
                order["refunded_at"] = int(time.time())
                order["refund_handler"] = clean_uid
                order["refund_note"] = "force_refund"
                self._save_data()
                await self._send_private_notice(
                    event,
                    order["user_id"],
                    f"↩️ 你的积分商城订单 {order_id} 已由超级管理员强制退款，{order.get('price', 0)} 积分已退回。\n商品：{order['item_name']}\n当前积分：{buyer['points']}\n说明：已发出的卡密/内容不会自动回收，已售内容删除流程仍按原计划执行。",
                )
                yield self._plain_result(event, f"订单 {order_id} 已强制退款，已退回 {order.get('price', 0)} 积分。注意：已发出的卡密/内容不会自动回收，已售内容删除流程仍按原计划执行。")
                return

        yield self._plain_result(event, f"没有找到订单号为 {order_id} 的订单。")

    @filter.command("购买")
    async def buy_item(self, event: AstrMessageEvent, item_id: int):
        if not self._is_private_event(event):
            yield self._plain_result(event, "主人，为了隐私，请到私聊中找我购买商品哦！")
            return
        async with self.purchase_lock:
            user_id = self._clean_user_id(event.get_sender_id())
            user = self._get_user(user_id)
            target_item = self._find_item(item_id)
            if target_item and target_item.get("status") != "approved":
                target_item = None
            if not target_item:
                yield self._plain_result(event, f"没找到序号为 {item_id} 的商品，或者它还没有上架哦。")
                return
            if user["points"] < target_item["price"]:
                yield self._plain_result(event, f"呜呜，你的积分不够呢。该商品需要 {target_item['price']} 积分，你只有 {user['points']} 积分。")
                return

            delivery_type = target_item.get("delivery_type", "stock")
            delivery_content = ""
            delivery_stock_id = 0
            delivery_version = int(target_item.get("delivery_version", 0))
            if delivery_type == "fixed":
                delivery_content = target_item.get("delivery_content", "")
                if not delivery_content:
                    yield self._plain_result(event, "该商品还没有配置自动发货内容，请联系管理员补充。")
                    return
            else:
                if not self._available_stock(target_item):
                    yield self._plain_result(event, "该商品库存不足，暂时无法购买，请联系管理员补货。")
                    return

            user["points"] -= target_item["price"]
            self.points_data["order_counter"] += 1
            order_id = self.points_data["order_counter"]
            if delivery_type == "stock":
                stock_item = self._take_stock(target_item, user_id, order_id)
                if not stock_item:
                    user["points"] += target_item["price"]
                    yield self._plain_result(event, "该商品库存刚刚被买完了，请稍后再试。")
                    return
                delivery_content = stock_item.get("content", "")
                delivery_stock_id = stock_item.get("id", 0)
            delivery_hash = self._hash_text(delivery_content)
            order = {
                "order_id": order_id,
                "user_id": user_id,
                "item_id": target_item["id"],
                "item_name": target_item["name"],
                "price": target_item["price"],
                "desc": target_item.get("desc", ""),
                "creator": target_item.get("creator", ""),
                "status": "completed",
                "created_at": int(time.time()),
                "completed_at": int(time.time()),
                "handler": "auto",
                "delivery_mode": "auto",
                "delivery_type": delivery_type,
                "delivery_stock_id": delivery_stock_id,
                "delivery_version": delivery_version,
                "delivery_hash": delivery_hash,
            }
            self.points_data["orders"].append(order)
            self._log_points(user_id, -int(target_item["price"]), "purchase", "system", order_id, target_item["id"])
            self._save_data()

        await self._notify_auto_delivery_admins(event, order)
        if delivery_type == "stock":
            await self._notify_low_stock_admins(event, target_item, len(self._available_stock(target_item)))
        yield self._private_plain_result(event, 
            f"🎉 购买成功，已自动发货！\n"
            f"订单号：{order_id}\n"
            f"商品名称：{target_item['name']}\n"
            f"消耗积分：{target_item['price']}\n"
            f"剩余积分：{user['points']}\n"
            f"订单状态：已完成\n"
            f"\n📦 发货内容：\n{delivery_content}"
        )
        logger.info(f"用户 {user_id} 购买了商品 {target_item['name']}，订单号 {order_id}")
