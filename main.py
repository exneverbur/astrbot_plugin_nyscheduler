import asyncio
import datetime
import os
import tempfile
import traceback
from typing import Any, Tuple, List

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain


@register(
    "astrbot_nyscheduler",
    "柠柚",
    "这是 AstrBot 的一个定时推送插件。包含60s，摸鱼日历，今日金价，AI资讯。",
    "1.0.3",  # 版本升级
)
class Daily60sNewsPlugin(Star):
    """
    AstrBot 每日60s新闻插件，支持多时间点定时推送和命令获取。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.groups = self.config.groups
        # 支持多个时间点，如 "10:00,18:00"
        self.push_times = [t.strip() for t in getattr(self.config, "push_time", "08:00").split(",")]
        self.news_api = getattr(self.config, "news_api", "https://api.nycnm.cn/API/60s.php")
        self.format = getattr(self.config, "format", "image")
        self.moyu_format = getattr(self.config, "moyu_format", "image")
        self.moyu_api = getattr(self.config, "moyu_api", "https://api.nycnm.cn/API/moyu.php")
        self.enable_news = getattr(self.config, "enable_news", True)
        self.enable_moyu = getattr(self.config, "enable_moyu", True)
        self.enable_gold = getattr(self.config, "enable_gold", True)
        self.enable_ai = getattr(self.config, "enable_ai", True)
        self.gold_format = getattr(self.config, "gold_format", "image")
        self.gold_api = getattr(self.config, "gold_api", "https://api.nycnm.cn/API/jinjia.php")
        self.ai_format = getattr(self.config, "ai_format", "image")
        self.ai_api = getattr(self.config, "ai_api", "https://api.nycnm.cn/API/aizixun.php")
        self.api_key = getattr(self.config, "api_key", "")
        self.timeout = getattr(self.config, "timeout", 30)
        logger.info(f"插件配置: {self.config}")
        self._monitoring_task = asyncio.create_task(self._daily_task())

    def _parse_time(self, time_str: str) -> datetime.time:
        """解析 'HH:MM' 格式为 time 对象"""
        h, m = map(int, time_str.split(":"))
        return datetime.time(hour=h, minute=m)

    def _get_next_push_time(self) -> datetime.datetime:
        """返回距离现在最近的下一个推送时间点（datetime）"""
        now = datetime.datetime.now()
        candidates = []
        for t_str in self.push_times:
            try:
                t = self._parse_time(t_str)
                candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                if candidate <= now:
                    candidate += datetime.timedelta(days=1)
                candidates.append(candidate)
            except Exception as e:
                logger.warning(f"无效推送时间 '{t_str}': {e}")
        if not candidates:
            raise ValueError("没有有效的推送时间配置")
        return min(candidates)

    async def terminate(self):
        """插件卸载时调用"""
        tasks = [
            "_monitoring_task",
            "_moyu_task",
            "_gold_task",
            "_ai_task"
        ]
        for attr in tasks:
            if hasattr(self, attr):
                task = getattr(self, attr)
                if task and not task.done():
                    task.cancel()
        logger.info("每日60s新闻插件: 定时任务已停止")

    # ========== 公共方法：发送到群组 ==========
    async def _send_to_groups(self, fetch_func, is_image: bool):
        try:
            if is_image:
                path, ok = await fetch_func()
                if not ok:
                    raise Exception(str(path))
                for target in self.groups:
                    await self.context.send_message(target, MessageChain().file_image(path))
                    await asyncio.sleep(2)
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                content, ok = await fetch_func()
                if not ok:
                    raise Exception(str(content))
                for target in self.groups:
                    await self.context.send_message(target, MessageChain().message(content))
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"[推送] 失败: {e}")

    # ========== 新闻相关 ==========
    @filter.command_group("新闻管理")
    def mnews(self): pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("status")
    async def check_status(self, event: AstrMessageEvent):
        next_push = self._get_next_push_time()
        delta = next_push - datetime.datetime.now()
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        yield event.plain_result(
            f"每日60s新闻插件运行中\n"
            f"推送时间: {', '.join(self.push_times)}\n"
            f"格式: {self.format}\n"
            f"下次推送: {hours}小时{minutes}分钟后"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("push")
    async def push_news(self, event: AstrMessageEvent):
        await self._send_to_groups(self._fetch_news_image_path if self.format == "image" else self._fetch_news_text, self.format == "image")
        yield event.plain_result(f"{event.get_sender_name()}: 已推送新闻")

    @mnews.command("今日")
    async def get_today_news(self, event: AstrMessageEvent):
        await self._handle_fetch(event, self._fetch_news_image_path, self._fetch_news_text, self.format)

    @filter.command("新闻")
    async def cmd_news(self, event: AstrMessageEvent):
        await self.get_today_news(event)

    @filter.command("60s")
    async def cmd_60s(self, event: AstrMessageEvent):
        await self.get_today_news(event)

    @filter.command("60秒")
    async def cmd_60sec(self, event: AstrMessageEvent):
        await self.get_today_news(event)

    @filter.command("早报")
    async def cmd_morning_news(self, event: AstrMessageEvent):
        await self.get_today_news(event)

    async def _fetch_news_text(self) -> Tuple[str, bool]:
        retries = 3
        timeout = self.timeout
        fmt = "json" if self.format == "image" else "text"
        date = datetime.datetime.now().strftime("%Y-%m-%d")
        for attempt in range(retries):
            try:
                url = f"{self.news_api}?date={date}&format={fmt}"
                if self.api_key:
                    url += f"&apikey={self.api_key}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as response:
                        if response.status != 200:
                            raise Exception(f"API返回错误代码: {response.status}")
                        if fmt == "json":
                            data = await response.json()
                            payload = data.get("data", {})
                            date_str = payload.get("date") or date
                            tip = payload.get("tip") or ""
                            news_list = payload.get("news") or []
                            lines = [f"{date_str} 每日60秒新闻", *(f"• {item}" for item in news_list)]
                            if tip:
                                lines.append(f"提示：{tip}")
                            return "\n".join(lines), True
                        else:
                            content = await response.read()
                            return content.decode("utf-8", errors="ignore"), True
            except Exception as e:
                logger.error(f"[mnews] 请求失败 {attempt + 1}/{retries}: {e}")
                if attempt == retries - 1:
                    return f"接口报错，请联系管理员:{e}", False
                await asyncio.sleep(1)
        return "未知错误", False

    async def _fetch_news_image_path(self) -> Tuple[str, bool]:
        retries = 3
        timeout = self.timeout
        fmt = "json" if self.format == "text" else "image"
        date = datetime.datetime.now().strftime("%Y-%m-%d")
        for attempt in range(retries):
            try:
                url = f"{self.news_api}?date={date}&format={fmt}"
                if self.api_key:
                    url += f"&apikey={self.api_key}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as response:
                        if response.status != 200:
                            raise Exception(f"API返回错误代码: {response.status}")
                        if fmt == "json":
                            data = await response.json()
                            payload = data.get("data", {})
                            img_url = payload.get("image") or payload.get("cover")
                            if not img_url:
                                raise Exception("JSON中未找到图片URL")
                            async with session.get(img_url, timeout=timeout) as img_resp:
                                if img_resp.status != 200:
                                    raise Exception(f"图片下载失败，状态码: {img_resp.status}")
                                img_bytes = await img_resp.read()
                                f = tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg")
                                f.write(img_bytes)
                                f.flush()
                                f.close()
                                return f.name, True
                        else:
                            img_bytes = await response.read()
                            f = tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg")
                            f.write(img_bytes)
                            f.flush()
                            f.close()
                            return f.name, True
            except Exception as e:
                logger.error(f"[mnews] 请求失败 {attempt + 1}/{retries}: {e}")
                if attempt == retries - 1:
                    return f"接口报错，请联系管理员:{e}", False
                await asyncio.sleep(1)
        return "未知错误", False

    # ========== 摸鱼日历 ==========
    @filter.command_group("摸鱼管理")
    def moyu(self): pass

    @filter.command("摸鱼")
    async def cmd_moyu_simple(self, event: AstrMessageEvent):
        await self._handle_fetch(event, self._moyu_fetch_image_path, self._moyu_fetch_text, self.moyu_format)

    @filter.command("摸鱼日历")
    async def cmd_moyu_calendar(self, event: AstrMessageEvent):
        await self.cmd_moyu_simple(event)

    @moyu.command("今日")
    async def moyu_today(self, event: AstrMessageEvent):
        await self.cmd_moyu_simple(event)

    async def _moyu_fetch_text(self) -> Tuple[str, bool]:
        return await self._generic_fetch_text(self.moyu_api, self.moyu_format)

    async def _moyu_fetch_image_path(self) -> Tuple[str, bool]:
        return await self._generic_fetch_image_path(self.moyu_api, self.moyu_format)

    # ========== 金价 ==========
    @filter.command_group("金价管理")
    def gold(self): pass

    @filter.command("金价")
    async def cmd_gold_simple(self, event: AstrMessageEvent):
        await self._handle_fetch(event, self._gold_fetch_image_path, self._gold_fetch_text, self.gold_format)

    @filter.command("黄金")
    async def cmd_gold_alt(self, event: AstrMessageEvent):
        await self.cmd_gold_simple(event)

    @gold.command("今日")
    async def gold_today(self, event: AstrMessageEvent):
        await self.cmd_gold_simple(event)

    async def _gold_fetch_text(self) -> Tuple[str, bool]:
        return await self._generic_fetch_text(self.gold_api, self.gold_format)

    async def _gold_fetch_image_path(self) -> Tuple[str, bool]:
        return await self._generic_fetch_image_path(self.gold_api, self.gold_format)

    # ========== AI资讯 ==========
    @filter.command_group("AI资讯管理")
    def ai(self): pass

    @filter.command("AI资讯")
    async def cmd_ai_simple(self, event: AstrMessageEvent):
        await self._handle_fetch(event, self._ai_fetch_image_path, self._ai_fetch_text, self.ai_format)

    @filter.command("AI新闻")
    async def cmd_ai_news(self, event: AstrMessageEvent):
        await self.cmd_ai_simple(event)

    @ai.command("今日")
    async def ai_today(self, event: AstrMessageEvent):
        await self.cmd_ai_simple(event)

    async def _ai_fetch_text(self) -> Tuple[str, bool]:
        return await self._generic_fetch_text(self.ai_api, self.ai_format)

    async def _ai_fetch_image_path(self) -> Tuple[str, bool]:
        return await self._generic_fetch_image_path(self.ai_api, self.ai_format)

    # ========== 通用抓取方法 ==========
    async def _generic_fetch_text(self, api_url: str, fmt: str) -> Tuple[str, bool]:
        retries = 3
        timeout = self.timeout
        use_json = fmt == "image"
        for attempt in range(retries):
            try:
                url = f"{api_url}?format={'json' if use_json else 'text'}"
                if self.api_key:
                    url += f"&apikey={self.api_key}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            raise Exception(f"状态码: {resp.status}")
                        if use_json:
                            data = await resp.json(content_type=None)
                            txt = self._extract_first_string(data)
                            return txt or str(data), True
                        else:
                            content = await resp.read()
                            return content.decode("utf-8", errors="ignore"), True
            except Exception as e:
                logger.error(f"[通用文本] 请求失败 {attempt + 1}/{retries}: {e}")
                if attempt == retries - 1:
                    return f"接口报错: {e}", False
                await asyncio.sleep(1)
        return "未知错误", False

    async def _generic_fetch_image_path(self, api_url: str, fmt: str) -> Tuple[str, bool]:
        retries = 3
        timeout = self.timeout
        use_json = fmt == "text"
        for attempt in range(retries):
            try:
                url = f"{api_url}?format={'json' if use_json else 'image'}"
                if self.api_key:
                    url += f"&apikey={self.api_key}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            raise Exception(f"状态码: {resp.status}")
                        if use_json:
                            data = await resp.json(content_type=None)
                            img_url = self._extract_first_image_url(data)
                            if not img_url:
                                raise Exception("JSON未找到图片URL")
                            async with session.get(img_url, timeout=timeout) as ir:
                                if ir.status != 200:
                                    raise Exception(f"图片状态码: {ir.status}")
                                b = await ir.read()
                        else:
                            b = await resp.read()
                        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jpeg")
                        f.write(b)
                        f.flush()
                        f.close()
                        return f.name, True
            except Exception as e:
                logger.error(f"[通用图片] 请求失败 {attempt + 1}/{retries}: {e}")
                if attempt == retries - 1:
                    return f"接口报错: {e}", False
                await asyncio.sleep(1)
        return "未知错误", False

    def _extract_first_string(self, obj) -> str | None:
        if isinstance(obj, str):
            return obj
        elif isinstance(obj, dict):
            for v in obj.values():
                res = self._extract_first_string(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = self._extract_first_string(item)
                if res:
                    return res
        return None

    def _extract_first_image_url(self, obj) -> str | None:
        if isinstance(obj, str) and obj.startswith("http") and any(ext in obj for ext in (".jpg", ".jpeg", ".png")):
            return obj
        elif isinstance(obj, dict):
            for v in obj.values():
                res = self._extract_first_image_url(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = self._extract_first_image_url(item)
                if res:
                    return res
        return None

    async def _handle_fetch(self, event: AstrMessageEvent, image_func, text_func, fmt: str):
        try:
            if fmt == "image":
                path, ok = await image_func()
                if ok:
                    await event.send(MessageChain().file_image(path))
                    try:
                        os.remove(path)
                    except:
                        pass
                else:
                    await event.send(event.plain_result(str(path)))
            else:
                content, ok = await text_func()
                if ok:
                    await event.send(event.plain_result(content))
                else:
                    await event.send(event.plain_result(str(content)))
        except Exception as e:
            await event.send(event.plain_result(f"获取失败: {e}"))

    # ========== 定时任务主循环 ==========
    async def _daily_task(self):
        while True:
            try:
                next_push = self._get_next_push_time()
                sleep_seconds = (next_push - datetime.datetime.now()).total_seconds()
                logger.info(f"[定时推送] 下次推送将在 {sleep_seconds / 3600:.2f} 小时后 ({next_push})")
                await asyncio.sleep(max(sleep_seconds, 0))

                # 推送所有启用的内容
                if self.enable_news:
                    await self._send_to_groups(
                        self._fetch_news_image_path if self.format == "image" else self._fetch_news_text,
                        self.format == "image"
                    )
                if self.enable_moyu:
                    await self._send_to_groups(
                        self._moyu_fetch_image_path if self.moyu_format == "image" else self._moyu_fetch_text,
                        self.moyu_format == "image"
                    )
                if self.enable_gold:
                    await self._send_to_groups(
                        self._gold_fetch_image_path if self.gold_format == "image" else self._gold_fetch_text,
                        self.gold_format == "image"
                    )
                if self.enable_ai:
                    weekday = datetime.datetime.now().weekday()
                    if weekday not in (5, 6):  # 周六日不推（原逻辑是周日周一？这里按常理调整为周末）
                        await self._send_to_groups(
                            self._ai_fetch_image_path if self.ai_format == "image" else self._ai_fetch_text,
                            self.ai_format == "image"
                        )
                    else:
                        logger.info("[AI资讯] 周末不推送")

                await asyncio.sleep(60)  # 避免同一分钟内重复触发

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[nyscheduler] 定时任务出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(300)
