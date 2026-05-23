import json
import os
import random
import time
import asyncio
from typing import List, Dict, Any
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
import astrbot.api.message_components as Comp

class PointsMallPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_path = os.path.join(os.path.dirname(__file__), "points_data.json")
        self.points_data = self._load_data()
        self.active_cooldowns = {} # user_id -> last_reward_time

    def _load_data(self) -> Dict[str, Any]:
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"PointsMall 载入数据失败: {e}")
        return {
            "users": {},     # {user_id: {"points": 0, "last_sign": "YYYYMMDD"}}
            "items": [],     # [{"id": 1, "name": "...", "price": 10, "desc": "...", "status": "approved", "creator": "..."}]
            "item_counter": 0
        }

    def _save_data(self):
        try:
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.points_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"PointsMall 保存数据失败: {e}")

    def _get_user(self, user_id: str):
        if user_id not in self.points_data["users"]:
            self.points_data["users"][user_id] = {"points": 0, "last_sign": ""}
        return self.points_data["users"][user_id]

    def _is_super_admin(self, user_id: str) -> bool:
        return user_id in self.conf.get("super_admins", [])

    def _is_admin(self, user_id: str) -> bool:
        return user_id in self.conf.get("super_admins", []) or user_id in self.conf.get("admins", [])

    @filter.on_decorating_result()
    async def on_any_message(self, event: AstrMessageEvent):
        # 活跃积分逻辑
        if event.get_platform_name() != "qq": return # 暂只支持QQ或通用
        
        user_id = event.get_sender_id()
        text = event.get_plain_text().strip()
        
        # 字数判定
        if len(text) >= self.conf.get("active_text_len", 10):
            now = time.time()
            last_time = self.active_cooldowns.get(user_id, 0)
            if now - last_time >= self.conf.get("active_cooldown", 60):
                user = self._get_user(user_id)
                user["points"] += self.conf.get("active_point_reward", 1)
                self.active_cooldowns[user_id] = now
                self._save_data()
                # 默默增加不打扰，或者也可以在这里加个提示

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

    @filter.command("上架")
    async def add_item(self, event: AstrMessageEvent, name: str, price: int, description: str = ""):
        user_id = event.get_sender_id()
        if not self._is_admin(user_id):
            yield event.plain_result("只有管理员才能上架商品哦！")
            return
        
        self.points_data["item_counter"] += 1
        new_id = self.points_data["item_counter"]
        
        status = "approved" if self._is_super_admin(user_id) else "pending"
        
        item = {
            "id": new_id,
            "name": name,
            "price": price,
            "desc": description,
            "status": status,
            "creator": user_id
        }
        self.points_data["items"].append(item)
        self._save_data()
        
        if status == "approved":
            yield event.plain_result(f"成功上架商品！[序号 {new_id}] {name} - {price} 积分。")
        else:
            yield event.plain_result(f"商品 {name} 已提交，请等待超级管理员审核。")

    @filter.command("授权上架")
    async def approve_item(self, event: AstrMessageEvent, item_id: int):
        if not self._is_super_admin(event.get_sender_id()):
            yield event.plain_result("哼，这个权限只有超级管理员才有哦！")
            return
        
        for item in self.points_data["items"]:
            if item["id"] == item_id:
                item["status"] = "approved"
                self._save_data()
                yield event.plain_result(f"商品 [序号 {item_id}] {item['name']} 已授权通过！")
                return
        yield event.plain_result(f"没找到序号为 {item_id} 的商品。")

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
        
        user = self._get_user(target_qq)
        user["points"] += amount
        self._save_data()
        yield event.plain_result(f"成功为 {target_qq} 调整积分：{amount}。当前：{user['points']}。")

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
            yield event.plain_result(f"没找到序号为 {item_id} 的上架商品哦。")
            return
        
        if user["points"] < target_item["price"]:
            yield event.plain_result(f"呜呜，你的积分不够呢。需要 {target_item['price']}，你只有 {user['points']}。")
            return
        
        user["points"] -= target_item["price"]
        self._save_data()
        
        # 实际购买逻辑，这里只是模拟扣费成功
        yield event.plain_result(f"恭喜！成功购买商品：{target_item['name']}。\n消耗积分：{target_item['price']}，剩余积分：{user['points']}。")
        logger.info(f"用户 {user_id} 购买了商品 {target_item['name']}")
