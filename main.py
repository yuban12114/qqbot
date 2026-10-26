"""QQ AI 自动回复 - 命令行主程序（多模态 agent 版）。

通过 OneBot v11 协议（NapCat 等协议端）接入个人 QQ 账号，接收私聊 / 群聊的
文本、图片、语音与文件，交给 agent 编排层处理（含视觉理解、语音识别、联网搜索、
文生图、语音合成），再把生成的文本 / 图片 / 语音 / 文件发回。

数据流：
    QQ ↔ NapCat(OneBot WS) ↔ main.py(事件路由) ↔ agent(编排) ↔ LLM + services(手)
"""
import asyncio
import html as _html
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import websockets

import media
import services as svc
from ai_client import LLMClient
from agent import Agent
from config import load_config, timeout_of

logger = logging.getLogger("QQAIBot")

# CQ 码正则：匹配 @ 某人 与 任意 CQ 码（仅用于字符串型消息兜底）
CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
CQ_ANY_RE = re.compile(r"\[CQ:[^\]]*\]")


class OneBotClient:
    def __init__(self, config: dict):
        self.config = config
        ob = config["onebot"]
        self.ws_url = ob["ws_url"]
        self.token = ob.get("token", "") or ""
        self.bot_qq = str(ob.get("bot_qq", "") or "")
        # 三层：LLM（大脑）+ services（手）+ agent（编排）
        self.llm = LLMClient(config["openai"])
        self.services = svc.Services(config)
        self.agent = Agent(self.llm, self.services,
                           max_reply_length=int(config["reply_rules"].get("max_reply_length", 1500)),
                           recover_timeout=timeout_of(config, "recover_reply"))
        self.context: dict = {}
        self.last_reply: dict = {}
        self.login_info: dict = {}
        self._pending: dict = {}
        # 超时集中配置（config.json 的 timeouts 段）。非法值由 timeout_of 回落默认。
        #   onebot_api   —— 单次 API 调用的默认等待
        #   event_handle —— 一整条消息从下载到回复的总上限，防止单条事件无限占用 worker
        self._api_timeout = timeout_of(config, "onebot_api")
        self._event_timeout = timeout_of(config, "event_handle")
        # 事件队列：_loop 只负责读消息并把事件入队，实际处理由独立 worker 串行消费。
        # 这样 _handle_event 里调用 _call_api 等待响应时，读循环仍在跑，响应才收得到。
        self._event_queue: asyncio.Queue | None = None
        self.ws = None
        self.running = True
        self._warned_no_bot_qq = False
        self._login_refresh_task = None
        # 长期记忆持久化相关状态
        self._mem_enabled = False
        self._mem_path = None
        self._mem_save_interval = 30
        self._mem_dirty = False
        self._save_task = None

    # ------------------------- 连接与运行 -------------------------
    async def run(self):
        mem_cfg = self.config.get("memory", {})
        pers = mem_cfg.get("persistence", {})
        self._mem_enabled = bool(pers.get("enable", False))
        self._mem_path = self._resolve_mem_path(pers.get("path", "memory.json"))
        self._mem_save_interval = float(pers.get("save_interval_seconds", 30))
        if self._mem_enabled:
            self._load_memory()
            logger.info(
                "长期记忆已开启，存储路径: %s（每 %s 秒自动保存）",
                self._mem_path, self._mem_save_interval,
            )
            self._save_task = asyncio.create_task(self._autosave_loop())

        # 事件队列 + 消费者：跨重连保持，保证事件不丢、且读循环不被业务处理阻塞
        self._event_queue = asyncio.Queue()
        worker_task = asyncio.create_task(self._event_worker())
        house_task = asyncio.create_task(self._housekeeping_loop())

        try:
            while self.running:
                try:
                    logger.info("正在连接 OneBot WebSocket: %s", self.ws_url)
                    async with self._connect() as ws:
                        self.ws = ws
                        logger.info("WebSocket 连接成功")
                        loop_task = asyncio.create_task(self._loop())
                        await self._get_login_info()
                        await loop_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("连接或运行出错: %s，5 秒后重连...", exc)
                    await asyncio.sleep(5)
        finally:
            worker_task.cancel()
            house_task.cancel()
            if self._save_task is not None:
                self._save_task.cancel()
            self._save_memory(force=True)

    # ------------------------- 长期记忆持久化 -------------------------
    def _resolve_mem_path(self, name: str) -> Path:
        p = Path(name)
        if not p.is_absolute():
            p = Path(__file__).parent / p
        return p

    def _load_memory(self):
        if self._mem_path is None or not self._mem_path.exists():
            return
        try:
            with self._mem_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.context = data
                logger.info("已加载长期记忆：%d 个会话", len(self.context))
        except Exception as exc:
            logger.warning("加载记忆文件失败（将重新开始）：%s", exc)

    def _save_memory(self, force: bool = False):
        if not self._mem_enabled or self._mem_path is None:
            return
        if not force and not self._mem_dirty:
            return
        try:
            tmp = self._mem_path.parent / (self._mem_path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.context, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, self._mem_path)
            except OSError as exc:
                # Docker 把单个文件 bind mount 进来时（-v ./memory.json:/app/memory.json），
                # 该路径是挂载点，rename 覆盖会返回 EBUSY。此时退化为"就地去写"：
                # 用 copyfile 覆盖同一个 inode 的内容（挂载点仍有效），牺牲原子性换可用性。
                logger.debug("原子替换失败(%s)，回退为覆盖写入", exc)
                shutil.copyfile(tmp, self._mem_path)
                tmp.unlink(missing_ok=True)
            self._mem_dirty = False
        except Exception as exc:
            logger.warning("保存记忆文件失败：%s", exc)

    async def _autosave_loop(self):
        try:
            while self.running:
                await asyncio.sleep(self._mem_save_interval)
                self._save_memory()
        except asyncio.CancelledError:
            pass

    def _connect(self):
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if not headers:
            return websockets.connect(self.ws_url)
        try:
            return websockets.connect(self.ws_url, additional_headers=headers)
        except TypeError:
            return websockets.connect(self.ws_url, extra_headers=headers)

    async def _housekeeping_loop(self):
        """定期清理过期临时文件，防止 downloads/ 与 cache/ 无限增长把磁盘写满。

        磁盘写满会让所有媒体链路（下载/上传/转码）一起失效，且很难排查。
        TTL 默认 24 小时（config.cache_ttl_hours），只删超过时限的文件，
        正在发送或刚生成的图片/语音不会被误删。
        """
        ttl_h = float(self.config.get("cache_ttl_hours", 24) or 24)
        if ttl_h <= 0:
            logger.info("cache_ttl_hours <= 0，临时文件自动清理已禁用")
            return
        round_no = 0
        while True:
            await asyncio.sleep(3600)
            round_no += 1

            # 每 6 小时查一次方舟远端存储用量（总容量 20GB，默认 7 天自动过期）。
            # 保留远端文件是为了多轮追问能复用 file_id，但接近上限时必须提醒清理。
            if round_no % 6 == 1:
                try:
                    cnt, nbytes = await asyncio.to_thread(self.services.files_usage)
                    gb = nbytes / 1024 ** 3
                    if gb >= 16:
                        logger.warning(
                            "方舟远端存储已用 %.2f GB / 约 20 GB（%d 个文件），"
                            "建议运行 cleanup_remote.bat 清理", gb, cnt)
                    elif cnt:
                        logger.info("方舟远端存储：%d 个文件，%.2f GB", cnt, gb)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("查询远端存储用量失败: %s", exc)

            cutoff = time.time() - ttl_h * 3600
            dirs = [media._DOWNLOAD_DIR]
            cache_dir = getattr(self.services, "cache_dir", None)
            if cache_dir:
                dirs.append(cache_dir)
            removed = 0
            for d in dirs:
                try:
                    if not d.exists():
                        continue
                    for f in d.iterdir():
                        try:
                            if f.stat().st_mtime > cutoff:
                                continue
                            if f.is_file():
                                f.unlink(missing_ok=True)
                                removed += 1
                            elif f.is_dir() and f.name.startswith(("asr_seg_", "qqbot-media-")):
                                shutil.rmtree(f, ignore_errors=True)
                                removed += 1
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug("清理目录 %s 失败: %s", d, exc)

            # 系统临时目录里的视频下载残留：yt-dlp 失败时 workdir（qqbot-media-*）
            # 不会被清理，一次几十 MB，且不在 cache/downloads 覆盖范围内。
            try:
                tmp_root = Path(tempfile.gettempdir())
                for f in tmp_root.glob("qqbot-*"):
                    try:
                        if f.is_dir() and f.stat().st_mtime < cutoff:
                            shutil.rmtree(f, ignore_errors=True)
                            removed += 1
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

            # 容量上限：只看时间不够——某天连发几个大视频，2 周就能攒出几十 GB，
            # 磁盘满会让下载/上传/转码一起失效。因此再按总大小兜一层：
            # 超过 cache_max_gb 就按最旧开始删，直到降到上限的 90%。
            cap_gb = float(self.config.get("cache_max_gb", 5) or 0)
            if cap_gb > 0:
                entries = []
                for d in dirs:
                    try:
                        for f in d.iterdir():
                            if f.is_file():
                                st = f.stat()
                                entries.append((st.st_mtime, st.st_size, f))
                    except Exception:  # noqa: BLE001
                        continue
                total = sum(s for _, s, _ in entries)
                cap = int(cap_gb * 1024 ** 3)
                if total > cap:
                    entries.sort()
                    target = int(cap * 0.9)
                    for _, size, f in entries:
                        try:
                            f.unlink(missing_ok=True)
                            total -= size
                            removed += 1
                        except Exception:  # noqa: BLE001
                            pass
                        if total <= target:
                            break
                    logger.warning(
                        "临时文件超过 %.1fGB，已按最旧清理，当前约 %.2fGB",
                        cap_gb, total / 1024 ** 3)

            if removed:
                logger.info("已清理 %d 个过期临时文件/目录（TTL %.0f 小时）", removed, ttl_h)

    async def _event_worker(self):
        """串行消费事件队列。

        为什么要独立于 `_loop`：`_handle_event` 内部会 `await self._call_api(...)`
        等待 OneBot 的响应（如 get_msg / send_msg），而响应必须由 `_loop` 读到并
        调用 `_resolve_echo` 才能完成。若直接在 `_loop` 里处理事件，读循环被阻塞，
        响应永远收不到，只能等满超时（表现为每条回复凭空多等 10 秒、get_msg 静默失败）。

        超时（timeouts.event_handle）：单条事件最多占用 worker 这么久，到点取消并丢弃
        这一条，把 worker 让给后续事件。没有它的话，「队列不丢事件」会退化成「一条卡死
        的事件让所有会话无限等待」。注意 asyncio.to_thread 里的下载线程无法被取消，
        只是不再等它——worker 会被释放，后台线程跑完自行结束。
        """
        while True:
            data = await self._event_queue.get()
            started = time.monotonic()
            try:
                await asyncio.wait_for(self._handle_event(data),
                                       timeout=self._event_timeout)
            except asyncio.TimeoutError as exc:
                elapsed = time.monotonic() - started
                if elapsed >= self._event_timeout - 1:
                    # 外层总超时：放弃这一条，不重试（重试只会再次堵住队列，
                    # 而用户那边已经等了很久，重来一遍也拿不到及时回复）。
                    logger.warning(
                        "事件处理超时（>%.0f 秒），已放弃并继续；队列积压 %d 条；事件: %s；异常: %r",
                        self._event_timeout, self._event_queue.qsize(),
                        self._event_brief(data), exc,
                    )
                    await self._notify_failure(data, "timeout")
                else:
                    # 内部环节自己抛出的 TimeoutError，按普通异常处理，别误报成总超时
                    logger.exception("处理事件时内部超时（%.1f 秒）；事件: %s；异常: %s: %s",
                                     elapsed, self._event_brief(data),
                                     type(exc).__name__, exc)
                    await self._notify_failure(data, "error")
            except Exception as exc:  # noqa: BLE001
                # 带堆栈打印：异常类型 + 消息 + traceback，便于定位是哪一步炸的
                logger.exception("处理事件异常（已隔离，连接保持）；事件: %s；异常: %s: %s",
                                 self._event_brief(data), type(exc).__name__, exc)
                await self._notify_failure(data, "error")
            finally:
                self._event_queue.task_done()

    @staticmethod
    def _event_brief(data: dict) -> str:
        """超时/异常日志里用来定位事件的最小信息（不含聊天内容，避免日志泄露）。"""
        if not isinstance(data, dict):
            return repr(data)[:120]
        kind = (data.get("message_type") or data.get("notice_type")
                or data.get("post_type") or "?")
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        return (f"{kind} user={sender.get('user_id') or data.get('user_id') or '-'} "
                f"group={data.get('group_id') or '-'} "
                f"mid={data.get('message_id') or '-'}")

    # --------------------- 失败兜底：告知对方 + 记进上下文 ---------------------
    async def _notify_failure(self, data: dict, kind: str) -> None:
        """一条事件彻底跑不完（超时/异常）时：给对方一句简短说明，并把这轮写进记忆。

        为什么放在 worker 而不是 agent.handle 里：这段代码是在 wait_for 取消之后执行的，
        worker 自身没有被取消，才能安全地发消息、写记忆；在被取消的协程里 await 发送，
        随时可能再被打断。

        记忆也必须写。否则模型下一轮完全不知道"上一句是我回的处理失败提示"，
        用户追问"你刚才不是说超时了吗"时它会一脸茫然，甚至把提示当成用户说的话。
        这里只写"用户说了什么 + 我回了什么"，不写异常细节，避免污染后续上下文。
        """
        try:
            target = self._failure_target(data)
            if not target:
                return
            msg_type, user_id, group_id = target
            note = (
                f"（这条消息处理超过 {int(self._event_timeout)} 秒，我先放弃了。"
                f"可以重发一次，或者换个小一点的文件/视频。）"
                if kind == "timeout" else
                "（这条消息我这边处理出错了，已经记进日志。可以稍后再试一次。）"
            )
            try:
                await self._send_segments(msg_type, user_id, group_id,
                                          [{"type": "text", "data": {"text": note}}])
            except Exception as exc:  # noqa: BLE001
                logger.warning("发送失败说明出错（%s）: %s", self._event_brief(data), exc)
            self._remember_failure(msg_type, user_id, group_id, data, note)
        except Exception as exc:  # noqa: BLE001
            # 兜底本身绝不能再抛出：它跑在 worker 循环里，抛出会让整个消费循环退出
            logger.warning("失败兜底处理异常（%s）: %s", self._event_brief(data), exc)

    def _failure_target(self, data: dict):
        """失败说明该发给谁；返回 (msg_type, user_id, group_id) 或 None。

        只对"本来就会回复"的会话发：meta_event、自己发的消息、不认识的 notice
        一律不发，避免在群里刷无意义的消息。
        """
        if not isinstance(data, dict):
            return None
        post_type = data.get("post_type")
        if post_type == "message":
            msg_type = data.get("message_type")
            if msg_type not in ("private", "group"):
                return None
            sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
            user_id = str(sender.get("user_id") or data.get("user_id") or "")
            if msg_type == "private":
                return ("private", user_id, "") if user_id else None
            group_id = str(data.get("group_id") or "")
            return ("group", user_id, group_id) if group_id else None
        if post_type == "notice":
            ntype = data.get("notice_type")
            if ntype == "group_upload":
                group_id = str(data.get("group_id") or "")
                return ("group", str(data.get("user_id") or ""), group_id) if group_id else None
            if ntype == "offline_file":
                user_id = str(data.get("user_id") or "")
                return ("private", user_id, "") if user_id else None
        return None

    def _remember_failure(self, msg_type, user_id, group_id, data, note: str) -> None:
        """把"这一轮用户说了什么 + 我回了失败说明"写进会话记忆。"""
        sid = self._session_id(msg_type, user_id, group_id)
        entry = self.context.setdefault(sid, {"summary": "", "recent": []})
        user_msg = f"[{time.strftime('%m-%d %H:%M')}]\n{self._event_summary_text(data)}"
        recent = entry.setdefault("recent", [])
        # 正常路径下 agent.handle 成功后才写记忆，超时/异常通常一个字都还没写。
        # 万一取消恰好发生在"写完记忆、还没返回"的瞬间，这里会重复记一对，故先比对一次。
        if (len(recent) >= 2 and recent[-1].get("role") == "assistant"
                and (recent[-2].get("content") or "") == user_msg):
            return
        recent.append({"role": "user", "content": user_msg})
        recent.append({"role": "assistant", "content": note})
        self._mem_dirty = True

    def _event_summary_text(self, data: dict) -> str:
        """从原始事件里还原"用户说了什么"，供失败记账用（只取文本与媒体类型）。"""
        try:
            text, _at, images, voices, videos = self._parse_message(data.get("message", ""))
        except Exception:  # noqa: BLE001
            text, images, voices, videos = "", [], [], []
        marks = [f"[{label}×{len(seq)}]" for label, seq in
                 (("图片", images), ("语音", voices), ("视频", videos)) if seq]
        body = " ".join(marks + ([text.strip()] if (text or "").strip() else []))
        return body or "（这条消息没有可解析的文字内容）"

    async def _loop(self):
        # 注意：async for 本身的异常（WS 断开）要冒泡给 run() 触发重连；
        # 但「单条事件处理」的异常必须就地隔离，否则一条消息出错就会拖垮整个连接。
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "echo" in data and "status" in data:
                self._resolve_echo(data)
                continue
            # 交给 worker 处理，本循环立即回到读取状态（关键：保证 API 响应能被及时收到）
            await self._event_queue.put(data)

    # ------------------------- API 调用 -------------------------
    def _resolve_echo(self, data: dict):
        fut = self._pending.pop(data.get("echo"), None)
        if fut and not fut.done():
            fut.set_result(data)

    async def _call_api(self, action: str, params: dict, timeout: float | None = None):
        # timeout 为 None 时用 config.json 的 timeouts.onebot_api；个别调用点（如 get_login_info）
        # 仍可显式传更短的值。
        if timeout is None:
            timeout = self._api_timeout
        echo = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo}
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[echo] = fut
        try:
            await self.ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            # 关键：无论成功、超时还是 ws.send 抛 ConnectionClosed 都要清理。
            # 只在超时分支 pop 会让断线期间每条消息都留下一个僵尸 future，长期泄漏内存。
            self._pending.pop(echo, None)

    async def _get_login_info(self):
        try:
            resp = await self._call_api("get_login_info", {}, timeout=5)
            if resp and resp.get("status") == "ok":
                self.login_info = resp.get("data", {})
                if not self.bot_qq:
                    self.bot_qq = str(self.login_info.get("user_id", ""))
                logger.info(
                    "机器人登录成功: QQ=%s nickname=%s",
                    self.bot_qq, self.login_info.get("nickname"),
                )
        except Exception as exc:
            logger.warning("获取登录信息失败（可忽略，不影响收发）: %s", exc)

    # ------------------------- 事件处理 -------------------------
    async def _handle_event(self, data: dict):
        post_type = data.get("post_type")
        if post_type == "meta_event":
            return
        if post_type == "notice":
            await self._handle_notice(data)
            return
        if post_type != "message":
            return

        msg_type = data.get("message_type")
        sender = data.get("sender", {})
        user_id = str(sender.get("user_id", ""))
        group_id = str(data.get("group_id", "")) if msg_type == "group" else ""

        if self.config["reply_rules"].get("ignore_self", True):
            if self.bot_qq and user_id == self.bot_qq:
                return

        if (not self.bot_qq and msg_type == "group"
                and self.config["reply_rules"].get("group_at_only", True)):
            if self._login_refresh_task is None or self._login_refresh_task.done():
                self._login_refresh_task = asyncio.create_task(self._get_login_info())
            if not self._warned_no_bot_qq:
                self._warned_no_bot_qq = True
                logger.warning("bot_qq 尚未就绪，群聊 @ 检测可能失效；已后台重试获取登录信息。")

        if not self._should_reply(msg_type, user_id, group_id, data):
            return

        text, at_bot, image_datas, voice_datas, video_datas = self._parse_message(data.get("message", ""))

        # 排查用开关（config.debug_message_segments）：打印原始消息段结构，
        # 用来确认引用回复到底以什么形式上报（reply 段？还是别的字段）。
        if self.config.get("debug_message_segments"):
            raw = data.get("message", "")
            shown = (json.dumps(raw, ensure_ascii=False)
                     if not isinstance(raw, str) else raw)
            logger.info("原始消息段[%s]: %s", msg_type, shown[:900])

        # 引用回复：把被引用的原文取回来作为上下文，否则 LLM 不知道"他/这个"指什么
        quote, q_imgs, q_voices, q_videos, q_files = "", [], [], [], []
        reply_id = self._extract_reply_id(data.get("message", ""))
        if reply_id:
            quote, q_imgs, q_voices, q_videos, q_files = await self._fetch_quoted(reply_id)
            if quote:
                text = f"[引用的消息 · {quote}]\n{text}"

        # 合并转发：展开成结构化文本，并收集其中的媒体/文件段
        fwd_imgs, fwd_voices, fwd_videos, fwd_files = [], [], [], []
        for fid in self._extract_forward_ids(data.get("message", "")):
            ex = await self._expand_forward(fid)
            if ex["text"]:
                text = f"{text}\n{ex['text']}".strip()
            fwd_imgs += ex["images"]
            fwd_voices += ex["voices"]
            fwd_videos += ex["videos"]
            fwd_files += ex["files"]

        # 把图片/语音/视频段解析为本地路径（需 NapCat API 或 url）
        # 被引用消息里的媒体也算进来：群里常见"先发图/视频，再引用它 @机器人 问这是什么"
        #
        # segs 与 images/voices/videos 平行：记录该媒体在**本条消息**里是同类段的第几个，
        # -1 表示来自引用/转发（序号对不上，无法用 get_msg 定位重下）。
        # 有了它，远端 file_id 过期后可以回到 QQ 重新下载同一张图，而不是猜。
        images, voices, videos = [], [], []
        img_segs, voc_segs, vid_segs = [], [], []
        own_img_n, own_voc_n, own_vid_n = len(image_datas), len(voice_datas), len(video_datas)

        for idx, d in enumerate(list(image_datas) + list(q_imgs) + fwd_imgs):
            p = await media.fetch_image(d, self._call_api)
            if p:
                images.append(p)
                img_segs.append(idx if idx < own_img_n else -1)
        for idx, d in enumerate(list(voice_datas) + list(q_voices) + fwd_voices):
            p = await media.fetch_voice(d, self._call_api)
            if p:
                voices.append(p)
                voc_segs.append(idx if idx < own_voc_n else -1)
        for idx, d in enumerate(list(video_datas) + list(q_videos) + fwd_videos):
            p = await media.fetch_video(d, self._call_api)
            if p:
                videos.append(p)
                vid_segs.append(idx if idx < own_vid_n else -1)

        # 被引用的文件：下载后交给 agent 按扩展名路由（PDF/TXT/音频/Office 都支持）
        quoted_files = []
        for seg in q_files:
            if seg.get("kind") == "card":
                continue
            notice = {
                "file_name": seg.get("name") or "file",
                "file_id": seg.get("file_id") or "",
                "file_url": seg.get("url") or "",
                "group_id": group_id,
                "user_id": user_id,
                "busid": seg.get("busid"),
            }
            got = await media.fetch_file(notice, msg_type, self._call_api)
            if got:
                quoted_files.append(got)
            else:
                logger.warning("被引用文件下载失败：%s", seg.get("name") or seg.get("file_id"))

        # 当前消息里直接携带的文件段（用户把文件当消息发，不走 group_upload/offline_file 通知）
        # 合并转发里的文件段也一并处理
        own_segs = self._extract_file_segs(data.get("message", "")) + fwd_files
        own_files = []
        for seg in own_segs:
            if seg.get("kind") == "card":
                continue
            got = await self._resolve_file_seg(seg, msg_type, user_id, group_id)
            if got:
                own_files.append(got)
        all_files = own_files + quoted_files

        # 分享卡片（公众号/新闻/网盘链接）不是文件：不下载，把 url 交给 LLM 用 fetch_url 读，
        # 否则会白下一个 HTML 再回"该文件类型暂不支持解析"。
        card_urls = [s.get("url") for s in list(q_files) + list(own_segs)
                     if s.get("kind") == "card" and s.get("url")]
        if card_urls:
            text = (text + "\n" + "\n".join(f"[分享链接] {u}" for u in card_urls)).strip()

        if not text and not images and not voices and not videos and not all_files:
            return

        if msg_type == "group" and self.config["reply_rules"].get("group_sender_prefix", True):
            sender_name = sender.get("card") or sender.get("nickname") or user_id
            if text:
                text = f"{sender_name}：{text}"

        sid = self._session_id(msg_type, user_id, group_id)

        cd = self.config["reply_rules"].get("cooldown_seconds", 0) or 0
        now = time.time()
        if now - self.last_reply.get(sid, 0) < cd:
            logger.info("会话 %s 冷却中，跳过", sid)
            return
        self.last_reply[sid] = now

        user_input = {"text": text.strip(), "images": images, "voices": voices,
                      "videos": videos, "files": all_files,
                      "sender_name": sender.get("nickname", ""),
                      # 媒体在消息里的同类段序号（-1 = 来自引用/转发，无法用 get_msg 定位）。
                      # agent 用它登记"远端失效后回 QQ 重下"所需的消息定位。
                      "msg_id": data.get("message_id"),
                      "segs": {"images": img_segs, "voices": voc_segs, "videos": vid_segs}}
        # 这里刻意不 try/except：异常交给 _event_worker 统一处理（带堆栈打日志、
        # 给对方回一句简短说明、并把这轮记进上下文）。就地吞掉只会让用户彻底收不到回应。
        segments = await self.agent.handle(sid, user_input, self.context,
                                           self.config["memory"], self._call_api)

        if not segments:
            return
        self._mem_dirty = True
        try:
            await self._send_segments(msg_type, user_id, group_id, segments)
        except Exception as exc:  # noqa: BLE001
            # 发送失败（如 NapCat 已掉线、ws.send 抛 ConnectionClosed）只记录，
            # 不再向上冒泡拖垮整个 WebSocket 循环；等待 ws 层自然重连。
            logger.error("发送回复失败（连接可能已断开，将等待重连）: %s", exc)
        logger.info("已回复 [%s] 用户 %s: %s", msg_type, user_id, str(segments[0])[:40])

    async def _handle_notice(self, data: dict):
        ntype = data.get("notice_type")
        if ntype not in ("group_upload", "offline_file"):
            return
        rules = self.config["reply_rules"]
        if not rules.get("reply_to_files", True):
            return
        msg_type = "group" if ntype == "group_upload" else "private"
        if msg_type == "group" and not rules.get("enable_group", True):
            return
        if msg_type == "private" and not rules.get("enable_private", True):
            return

        group_id = str(data.get("group_id", "")) if msg_type == "group" else ""
        user_id = str(data.get("user_id", ""))
        if self.bot_qq and user_id == self.bot_qq:
            return

        # OneBot v11 的 group_upload / offline_file 把文件信息放在事件的 file 字段里：
        #   {"post_type":"notice","notice_type":"group_upload","group_id":..,"user_id":..,
        #    "file":{"id":"..","name":"..","size":..,"busid":..}}
        # 而 fetch_file 期望扁平字段，必须先拍平；否则 file_id/name 全是 None，整条链路静默跳过。
        finfo = dict(data.get("file") or {})
        finfo.setdefault("group_id", data.get("group_id"))
        finfo.setdefault("user_id", data.get("user_id"))
        finfo.setdefault("file_name", finfo.get("name"))
        finfo.setdefault("file_id", finfo.get("id"))
        finfo.setdefault("file_url", finfo.get("url"))
        result = await media.fetch_file(finfo, msg_type, self._call_api)
        if not result:
            logger.warning("无法获取文件内容，跳过：%s", finfo.get("file_name"))
            return

        sid = self._session_id(msg_type, user_id, group_id)
        cd = rules.get("cooldown_seconds", 0) or 0
        now = time.time()
        if now - self.last_reply.get(sid, 0) < cd:
            return
        self.last_reply[sid] = now

        name, path = result
        user_input = {"text": "", "images": [], "voices": [], "files": [(name, path)],
                      "sender_name": data.get("nickname", user_id)}
        try:
            segments = await self.agent.handle(sid, user_input, self.context,
                                                self.config["memory"], self._call_api)
        except Exception as exc:
            logger.error("Agent 处理文件失败: %s", exc)
            return
        if not segments:
            return
        self._mem_dirty = True
        try:
            await self._send_segments(msg_type, user_id, group_id, segments)
        except Exception as exc:  # noqa: BLE001
            logger.error("发送文件回复失败（连接可能已断开，将等待重连）: %s", exc)

    # ------------------------- 发送 -------------------------
    async def _send_segments(self, msg_type, user_id, group_id, segments):
        """把 agent 产出的消息段发出去。

        媒体分离回复（reply_rules.separate_media_replies，默认开启）：
          文字 / 图片 / 语音 / 文件 各自作为一条独立消息发出，
          避免挤在同一个气泡里（QQ 里图片、语音通常需要独立消息承载）。
        关闭时退回旧行为：文本+图片+语音合并为一条消息，文件单独上传。

        文件没有标准 message 段，始终单独走 upload_file 上传到群/私聊文件区。
        """
        rules = self.config["reply_rules"]
        separate = bool(rules.get("separate_media_replies", True))
        interval = float(rules.get("media_send_interval", 0.3))
        private = (msg_type == "private")
        base = ({"message_type": "private", "user_id": int(user_id)} if private
                else {"message_type": "group", "group_id": int(group_id)})

        async def _send_one(seg):
            if seg.get("type") == "file":
                data = seg.get("data", {})
                # OneBot v11 没有 upload_file 这个通用动作，群/私聊分两个 API。
                # 注意参数名是 `file`（本地路径），不是 `path`——NapCat 按规范只认 file。
                action = "upload_private_file" if private else "upload_group_file"
                params = {
                    "file": data.get("file"), "name": data.get("name", "file"),
                }
                if private:
                    params["user_id"] = int(user_id)
                else:
                    params["group_id"] = int(group_id)
                resp = await self._call_api(action, params, timeout=60)
            else:
                action = "send_msg"
                params = dict(base)
                params["message"] = [seg]
                resp = await self._call_api(action, params, timeout=30)
            # 不再静默丢弃发送结果：超时返回 None、风控/禁言返回 failed，
            # 以前统统不看，日志显示"已回复"但用户没收到，完全无法排查。
            if not isinstance(resp, dict) or resp.get("status") != "ok":
                logger.warning("发送未确认成功（%s，%s）：%s",
                               action, seg.get("type"), str(resp)[:200])

        if separate:
            first = True
            for seg in segments:
                if not first and interval > 0:
                    await asyncio.sleep(interval)
                await _send_one(seg)
                first = False
        else:
            media_segs = [s for s in segments if s.get("type") != "file"]
            if media_segs:
                params = dict(base)
                params["message"] = media_segs
                await self._call_api("send_msg", params)
            for fs in segments:
                if fs.get("type") == "file":
                    await _send_one(fs)

    # ------------------------- 规则与解析 -------------------------
    def _should_reply(self, msg_type, user_id, group_id, data) -> bool:
        rules = self.config["reply_rules"]
        if msg_type == "private":
            if not rules.get("enable_private", True):
                return False
            wl = [str(x) for x in rules.get("friend_whitelist", [])]
            if wl and user_id not in wl:
                return False
            return True
        if msg_type == "group":
            if not rules.get("enable_group", True):
                return False
            wl = [str(x) for x in rules.get("group_whitelist", [])]
            if wl and group_id not in wl:
                return False
            if rules.get("group_at_only", True):
                _, at_bot, _, _, _ = self._parse_message(data.get("message", ""))
                if not at_bot:
                    return False
            return True
        return False

    @staticmethod
    def _parse_cq_segments(text: str, seg_type: str) -> list:
        """从 CQ 码字符串里提取指定类型的段，返回 data dict 列表。

        例：`[CQ:image,file=a.jpg,url=https://...]` -> [{"file": "a.jpg", "url": "..."}]
        用于 NapCat 以字符串形式（而非消息段数组）返回消息时兜底。
        """
        out = []
        for m in re.finditer(rf"\[CQ:{seg_type}(?:,([^\]]*))?\]", text or ""):
            attrs = {}
            for kv in (m.group(1) or "").split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    attrs[k.strip()] = _html.unescape(v.strip())
            out.append(attrs)
        return out

    @staticmethod
    def _extract_file_segs(message):
        """从消息里提取文件段，返回 [{name, file_id, url, busid}]。

        QQ 的文件在不同协议端下形式不一，这里覆盖三种常见形态：
          1) 标准段：{"type":"file","data":{"file":"a.pdf","file_id":"...","url":"..."}}
          2) CQ 字符串：[CQ:file,file=a.pdf,file_id=...,url=...]
          3) JSON 卡片：{"type":"json","data":{"data":"{...}"}}，从中挖标题与跳转链接
        取不到的字段留空，由 NapCat API 侧兜底。
        """
        segs = []

        def _push(d, kind="file"):
            if not isinstance(d, dict):
                return
            name = d.get("name") or d.get("file") or ""
            fid = d.get("file_id") or d.get("id") or ""
            url = d.get("url") or d.get("file_url") or ""
            busid = d.get("busid")
            if name or fid or url:
                # kind="card" 表示这是分享卡片（公众号/新闻/文件链接），不是真文件：
                # 调用方应把 url 交给链接抓取，而不是当文件下载（否则白下一份 HTML）。
                segs.append({"name": name, "file_id": fid, "url": url,
                             "busid": busid, "kind": kind})

        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                t = seg.get("type")
                d = seg.get("data") or {}
                if t == "file":
                    _push(d)
                elif t == "json":
                    raw = d.get("data")
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except (json.JSONDecodeError, TypeError):
                        continue
                    meta = (obj or {}).get("meta") or {}
                    for key in ("detail_1", "news", "music", "dataline"):
                        detail = meta.get(key) or {}
                        if not isinstance(detail, dict):
                            continue
                        url = detail.get("qqdocurl") or detail.get("jumpUrl") or ""
                        title = detail.get("title") or detail.get("desc") or ""
                        if url or title:
                            _push({"name": title, "url": url}, kind="card")
                            break
        else:
            raw = str(message)
            for attrs in OneBotClient._parse_cq_segments(raw, "file"):
                _push(attrs)
        return segs

    async def _resolve_file_seg(self, seg: dict, msg_type: str, user_id, group_id):
        """把消息里携带的 file 段落到本地，返回 (name, path) 或 None。

        与群文件/离线文件通知不同：这类 file 段只给 file_id，必须用对应 URL 接口
        换取下载地址。按官方定义：私聊用 get_private_file_url、群聊用
        get_group_file_url，拿不到地址时私聊再退回通用接口 get_file。
        实测私聊发文件时消息段是纯 file 段（无 url），需要 get_private_file_url。
        """
        name = seg.get("name") or "file"
        suffix = Path(name).suffix or ".bin"
        url = seg.get("url") or ""
        if url:
            try:
                return name, await asyncio.to_thread(media._download, url, suffix)
            except Exception as exc:  # noqa: BLE001
                logger.warning("文件段直接下载失败(%s): %s", name, exc)

        fid = seg.get("file_id") or ""
        if not fid:
            return None

        if msg_type == "private":
            attempts = [
                ("get_private_file_url", {"user_id": int(user_id), "file_id": fid}),
                # get_file 是官方通用入口（file_id → data.file / data.url），
                # 不是 go-cqhttp 的 get_offline_file——NapCat 没有那个 action。
                ("get_file", {"file_id": fid}),
            ]
        else:
            attempts = [
                ("get_group_file_url", {"group_id": int(group_id), "file_id": fid,
                                        "busid": int(seg.get("busid") or 0)}),
            ]

        for action, params in attempts:
            try:
                resp = await self._call_api(action, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 调用失败: %s", action, exc)
                continue
            if isinstance(resp, dict) and resp.get("status") == "ok":
                path = await media._from_api_data(resp.get("data") or {}, suffix, action)
                if path:
                    return name, path
            else:
                logger.debug("%s 未返回可用文件: %s", action, str(resp)[:120])

        logger.warning("无法获取文件下载地址：%s（file_id=%s，尝试过 %s）",
                       name, fid, [a for a, _ in attempts])
        return None

    @staticmethod
    def _extract_forward_ids(message):
        """提取合并转发（forward）段的 id 列表。"""
        ids = []
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "forward":
                    fid = (seg.get("data") or {}).get("id")
                    if fid:
                        ids.append(str(fid))
        else:
            for attrs in OneBotClient._parse_cq_segments(str(message), "forward"):
                if attrs.get("id"):
                    ids.append(str(attrs["id"]))
        return ids

    async def _expand_forward(self, forward_id: str, depth: int = 0) -> dict:
        """展开合并转发的聊天记录，返回 {text, images, voices, videos, files}。

        返回的都是**消息段**（不是本地路径），由调用方统一下载，保持与其他路径一致。
        嵌套的子聊天记录递归展开，最多 2 层、每层最多 60 条，避免异常数据导致爆栈或刷屏。
        """
        out = {"text": "", "images": [], "voices": [], "videos": [], "files": []}
        if depth > 1:
            out["text"] = "[嵌套过深，已省略]"
            return out
        try:
            resp = await self._call_api("get_forward_msg", {"id": forward_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_forward_msg 失败(%s): %s", forward_id, exc)
            out["text"] = "[合并转发的内容无法展开]"
            return out
        data = (resp or {}).get("data") or {}
        nodes = data.get("messages") or data.get("message") or data.get("content") or []
        if not isinstance(nodes, list) or not nodes:
            logger.warning("get_forward_msg 返回结构未识别(%s): %s",
                           forward_id, json.dumps(resp, ensure_ascii=False)[:300])
            out["text"] = "[合并转发内容为空或格式不支持]"
            return out

        lines = []
        for i, node in enumerate(nodes[:60], 1):
            if not isinstance(node, dict):
                continue
            nd = node.get("data") or {}
            sender = node.get("sender") or {}
            name = (sender.get("card") or sender.get("nickname")
                    or nd.get("name") or nd.get("nickname") or "")
            content = (node.get("message") or nd.get("content")
                       or node.get("content") or [])
            t, _, imgs, vos, vids = self._parse_message(content)
            fsegs = self._extract_file_segs(content)
            sub_ids = self._extract_forward_ids(content)

            parts = []
            if t.strip():
                parts.append(t.strip())
            if imgs:
                parts.append(f"[{len(imgs)}张图片]")
                out["images"].extend(imgs)
            if vos:
                parts.append(f"[{len(vos)}条语音]")
                out["voices"].extend(vos)
            if vids:
                parts.append(f"[{len(vids)}个视频]")
                out["videos"].extend(vids)
            if fsegs:
                names = "、".join((s.get("name") or "文件") for s in fsegs[:3])
                parts.append(f"[{len(fsegs)}个文件：{names}]")
                out["files"].extend(fsegs)
            for sid in sub_ids:
                sub = await self._expand_forward(sid, depth + 1)
                if sub["text"]:
                    parts.append("\n   " + sub["text"].replace("\n", "\n   "))
                out["images"].extend(sub["images"])
                out["voices"].extend(sub["voices"])
                out["videos"].extend(sub["videos"])
                out["files"].extend(sub["files"])

            lines.append(f"{i}. {name}：{' '.join(parts) if parts else '（空消息）'}")

        total = len(nodes)
        shown = len(lines)
        more = "" if shown >= total else f"（仅展开前 {shown} 条）"
        out["text"] = f"[合并转发的聊天记录，共 {total} 条{more}]\n" + "\n".join(lines)
        logger.info("合并转发 %s 已展开：%d 条，图片%d/语音%d/视频%d/文件%d",
                    forward_id, total, len(out["images"]), len(out["voices"]),
                    len(out["videos"]), len(out["files"]))
        return out

    def _parse_message(self, message):
        """解析 OneBot 消息，返回 (纯文本, 是否@机器人, 图片段列表, 语音段列表, 视频段列表)。"""
        at_bot = False
        if isinstance(message, list):
            text_parts, images, voices, videos = [], [], [], []
            for seg in message:
                t = seg.get("type")
                if t == "text":
                    text_parts.append(seg.get("data", {}).get("text", ""))
                elif t == "at":
                    qq = str(seg.get("data", {}).get("qq", ""))
                    if self.bot_qq and qq == self.bot_qq:
                        at_bot = True
                elif t == "image":
                    images.append(seg.get("data", {}))
                elif t == "record":
                    voices.append(seg.get("data", {}))
                elif t == "video":
                    videos.append(seg.get("data", {}))
            return "".join(text_parts), at_bot, images, voices, videos
        # 字符串型兜底（NapCat 有时返回 CQ 码字符串）：文本、@ 与三类媒体都要解析，
        # 否则被引用的图片/视频会全部丢失，用户只能听到"1张图片"这种空答复。
        raw = str(message)
        for m in CQ_AT_RE.finditer(raw):
            if self.bot_qq and m.group(1) == self.bot_qq:
                at_bot = True
        return (CQ_ANY_RE.sub("", raw).strip(), at_bot,
                self._parse_cq_segments(raw, "image"),
                self._parse_cq_segments(raw, "record"),
                self._parse_cq_segments(raw, "video"))

    @staticmethod
    def _extract_reply_id(message):
        """从消息段里取出引用回复的消息 ID（没有引用返回 None）。"""
        if isinstance(message, list):
            for seg in message:
                if seg.get("type") == "reply":
                    rid = (seg.get("data") or {}).get("id")
                    if rid is not None and str(rid).strip():
                        return str(rid)
            return None
        m = re.search(r"\[CQ:reply,id=(-?\w+)\]", str(message))
        return m.group(1) if m else None

    async def _fetch_quoted(self, reply_id: str):
        """取被引用消息，返回 (摘要文本, 图片段, 语音段, 视频段, 文件段)。

        为什么要调 get_msg：OneBot 的 reply 段只带消息 ID，不带原文，
        不主动查询的话 LLM 看不到被引用的内容，也就无法理解"他刚说的""这个文件"。
        各类媒体/文件段都**原样返回**给调用方下载后送对应模型——否则用户"引用一个
        文件问讲了什么"时只能得到没有信息量的答复。
        """
        try:
            resp = await self._call_api("get_msg", {"message_id": reply_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取被引用消息失败 %s: %s", reply_id, exc)
            return "", [], [], [], []
        data = (resp or {}).get("data") or {}
        if not data:
            # 留痕：否则调用方只能看到"引用没生效"，无法判断是超时还是接口报错
            logger.warning("get_msg 未返回数据（reply_id=%s）: %s", reply_id,
                           json.dumps(resp, ensure_ascii=False)[:300] if resp else "无响应/超时")
            return "", [], [], [], []

        sender = data.get("sender") or {}
        name = (sender.get("card") or sender.get("nickname")
                or str(data.get("user_id") or ""))
        raw_msg = data.get("message") or data.get("raw_message") or ""
        qtext, _, qimg, qvoice, qvideo = self._parse_message(raw_msg)
        qfiles = self._extract_file_segs(raw_msg)
        qtext = (qtext or "").strip()

        extras = []
        if qimg:
            extras.append(f"{len(qimg)}张图片")
        if qvoice:
            extras.append(f"{len(qvoice)}条语音")
        if qvideo:
            extras.append(f"{len(qvideo)}个视频")
        if qfiles:
            names = "、".join((f.get("name") or "文件") for f in qfiles[:3])
            extras.append(f"{len(qfiles)}个文件（{names}）")
        body = " ".join([x for x in (qtext, "、".join(extras)) if x]) or "（无文本内容）"
        summary = f"{name}：{body}" if name else body
        logger.info("被引用消息 %s -> %s（图片%d/语音%d/视频%d/文件%d）", reply_id, summary[:80],
                    len(qimg), len(qvoice), len(qvideo), len(qfiles))
        return summary, qimg, qvoice, qvideo, qfiles

    @staticmethod
    def _session_id(msg_type, user_id, group_id) -> str:
        if msg_type == "group":
            return f"g{group_id}"
        return f"p{user_id}"


def main():
    cfg_path = Path(__file__).parent / "config.json"
    config = load_config(cfg_path)

    logging.basicConfig(
        level=getattr(logging, str(config.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not config["openai"].get("api_key"):
        logger.warning("未配置 LLM api_key，请在 config.json 中填写后重启。")
    if not config.get("services", {}).get("openai", {}).get("api_key"):
        logger.warning(
            "未配置 services.openai.api_key：图片/语音/文生图/语音合成将不可用，"
            "自动降级为纯文本（联网搜索仍可用）。"
        )

    client = OneBotClient(config)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止...")


if __name__ == "__main__":
    main()
