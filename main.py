import json
import os
import random
import time
import re
from typing import List, Dict, Any
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger

class PointsMallPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 数据文件统一存放在插件专属的数据目录下，防止升级或容器迁移时丢失
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_points_mall")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_path = os.path.join(self.data_dir, "points_data.json")
        self.points_data = self._load_data()
        self.active_cooldowns = {} # user_id -> last_reward_time (仅保存在内存中)

    def _load_data(self) -> Dict[str, Any]:
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 兼容旧版本数据结构
                    if "users" not in data: data["users"] = {}
                    if "items" not in data: data["items"] = []
                    if "item_counter" not in data: data["item_counter"] = 0
                    return data
            except Exception as e:
                logger.error(f"PointsMall 载入数据失败: {e}")
        return {
            "users": {},     # {user_id: {"points": 0, "last_sign": "YYYYMMDD"}}
            "items": [],     # [{"id": 1, "name": "...", "price": 10, "desc": "...", "status": "approved"/"pending", "creator": "..."}]
            "item_counter": 0
        }

    def _save_data(self):
        try:
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.points_data, f, ensure_ascii=False, indent=2)
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

    @filter.on_decorating_result()
    async def on_any_message(self, event: AstrMessageEvent):
        # 只在群聊消息中积攒活跃积分，私聊不计算
        if event.is_private():
            return
            
        user_id = event.get_sender_id()
        if not user_id:
            return
            
        text = event.get_plain_text().strip()
        
        # 过滤掉指令、签到、购买等消息，防止刷屏指令获取积分
        if text.startswith(("/", "!", "！", "购买", "签到", "我的积分", "积分商场", "积分排行", "给予积分")):
            return
            
        # 字数判定
        if len(text) >= self.conf.get("active_text_len", 10):
            now = time.time()
            last_time = self.active_cooldowns.get(user_id, 0)
            if now - last_time >= self.conf.get("active_cooldown", 60):
                user = self._get_user(user_id)
                user["points"] += self.conf.get("active_point_reward", 1)
                self.active_cooldowns[user_id] = now
                self._save_data()

    @filter.command("签到")
    async def sign_in(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        user = self._get_user(user_id)
        today = time.strftime("%Y%m%d")
        
        if user["last_sign"] == today:
            yield event.plain_result("主人，你今天已经签到过了哦~ 不要太贪心嘛。")
            return
        
        gain = random.randint(self.conf.get("sign_min", 10), self.conf.get("sign_max", 50))
        user["points"] += gain
        user["last_sign"] = today
        self._save_data()
        yield event.plain_result(f"签到成功！获得 {gain} 积分。当前总积分：{user['points']}。")

    @filter.command("我的积分")
    async def my_points(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        user = self._get_user(user_id)
        yield event.plain_result(f"主人，你目前的积分为：{user['points']} 点。")

    @filter.command("积分排行")
    async def points_leaderboard(self, event: AstrMessageEvent):
        users = self.points_data.get("users", {})
        if not users:
            yield event.plain_result("目前还没有任何人有积分记录哦~")
            return
            
        # 按照积分从高到低排序
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        top_10 = sorted_users[:10]
        
        msg = "🏆 === 积分龙虎榜 === 🏆\n"
        for idx, (uid, info) in enumerate(top_10, 1):
            msg += f"第 {idx} 名: QQ {uid} | {info.get('points', 0)} 积分\n"
        yield event.plain_result(msg.strip())

    @filter.command("上架")
    async def add_item(self, event: AstrMessageEvent, name: str, price: int, description: str = ""):
        user_id = event.get_sender_id()
        if not self._is_admin(user_id):
            yield event.plain_result("只有管理员才能上架商品哦！")
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
            "creator": "".join(re.findall(r"\d+", str(user_id)))
        }
        self.points_data["items"].append(item)
        self._save_data()
        
        if status == "approved":
            yield event.plain_result(f"成功上架商品！[序号 {new_id}] {name} - {price} 积分。")
        else:
            yield event.plain_result(f"商品「{name}」已提交申请（序号 {new_id}），请等待超级管理员授权上架。")

    @filter.command("待审核商品")
    async def pending_list(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        if not self._is_admin(user_id):
            yield event.plain_result("只有管理员才能查看待审核商品列表哦！")
            return
            
        pending_items = [i for i in self.points_data["items"] if i["status"] == "pending"]
        if not pending_items:
            yield event.plain_result("当前没有待审核的商品~")
            return
            
        msg = "📋 === 待审核商品列表 === 📋\n"
        for item in pending_items:
            msg += f"序号 {item['id']}. {item['name']} | {item['price']} 积分\n   描述: {item['desc']}\n   申请人: QQ {item['creator']}\n"
        msg += "\n* 超级管理员可发送 '授权上架 [序号]' 或 '下架 [序号]'"
        yield event.plain_result(msg.strip())

    @filter.command("授权上架")
    async def approve_item(self, event: AstrMessageEvent, item_id: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield event.plain_result("哼，这个权限只有超级管理员才有哦！")
            return
        
        for item in self.points_data["items"]:
            if item["id"] == item_id:
                if item["status"] == "approved":
                    yield event.plain_result(f"商品 [序号 {item_id}] 已经是上架状态啦。")
                    return
                item["status"] = "approved"
                self._save_data()
                yield event.plain_result(f"商品 [序号 {item_id}]「{item['name']}」已授权通过，正式开售！")
                return
        yield event.plain_result(f"没找到序号为 {item_id} 的商品。")

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
            yield event.plain_result(f"没有找到序号为 {item_id} 的商品。")
            return
            
        # 只有超级管理员，或者该商品的创建者（且为管理员）可以下架
        if self._is_super_admin(user_id) or (target_item["creator"] == clean_uid and self._is_admin(user_id)):
            self.points_data["items"].remove(target_item)
            self._save_data()
            yield event.plain_result(f"商品 [序号 {item_id}]「{target_item['name']}」已成功下架/删除！")
        else:
            yield event.plain_result("哼，你没有权限下架这个商品哦！")

    @filter.command("积分商场")
    async def mall_list(self, event: AstrMessageEvent):
        msg = "=== 积分商场 ===\n"
        approved_items = [i for i in self.points_data["items"] if i["status"] == "approved"]
        if not approved_items:
            msg += "目前商场空空如也呢..."
        else:
            for item in approved_items:
                msg += f"{item['id']}. {item['name']} | {item['price']} 积分\n   描述: {item['desc']}\n"
        msg += "\n* 请私聊我发送 '购买 [序号]' 进行购买~"
        yield event.plain_result(msg.strip())

    @filter.command("给予积分")
    async def give_points(self, event: AstrMessageEvent, target_qq: str, amount: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield event.plain_result("权限不足。")
            return
            
        # 提取 target_qq 中的所有数字（适配@或者纯QQ）
        clean_qq = "".join(re.findall(r"\d+", target_qq))
        if not clean_qq:
            yield event.plain_result("请输入有效的QQ号或艾特目标成员哦！")
            return
            
        user = self._get_user(clean_qq)
        user["points"] += amount
        self._save_data()
        
        action = "给予" if amount >= 0 else "扣除"
        abs_amount = abs(amount)
        yield event.plain_result(f"成功为 QQ {clean_qq} {action} {abs_amount} 积分。当前总积分：{user['points']}。")

    @filter.command("购买")
    async def buy_item(self, event: AstrMessageEvent, item_id: int):
        if not event.is_private():
            yield event.plain_result("主人，为了隐私，请到私聊中找我购买商品哦！")
            return
        
        user_id = event.get_sender_id()
        user = self._get_user(user_id)
        
        target_item = None
        for item in self.points_data["items"]:
            if item["id"] == item_id and item["status"] == "approved":
                target_item = item
                break
        
        if not target_item:
            yield event.plain_result(f"没找到序号为 {item_id} 的商品，或者它还没有上架哦。")
            return
        
        if user["points"] < target_item["price"]:
            yield event.plain_result(f"呜呜，你的积分不够呢。该商品需要 {target_item['price']} 积分，你只有 {user['points']} 积分。")
            return
        
        user["points"] -= target_item["price"]
        self._save_data()
        
        # 实际购买逻辑，这里扣费成功后可根据需要扩展回调
        yield event.plain_result(f"🎉 购买成功！\n商品名称：{target_item['name']}\n消耗积分：{target_item['price']}\n剩余积分：{user['points']}。")
        logger.info(f"用户 {user_id} 购买了商品 {target_item['name']}")
