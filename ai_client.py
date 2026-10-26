"""LLM 调用层：通过 OpenAI 兼容 Chat Completions 接口调用大模型（默认 DeepSeek），
并维护多轮对话记忆与自动摘要。

本模块只负责"调用模型 + 记忆"，不关心多模态——多模态的输入归一化与输出生成
由 agent.py 编排。记忆策略（config["memory"]）：
- mode="accumulate"（默认）：全量累积近期对话，仅当估算 token 接近上限时自动摘要压缩。
- mode="sliding"：仅保留最近 max_history 轮。

另含 DeepSeek prompt cache 命中/未命中日志（命中仅 ¥0.02/百万，未命中 ¥1/百万）。
"""
import asyncio
import json
import logging
import urllib.request

logger = logging.getLogger("QQAIBot.AI")


class LLMClient:
    def __init__(self, cfg: dict):
        self.api_key = cfg.get("api_key", "") or ""
        base = (cfg.get("base_url") or "https://api.openai.com/v1").strip()
        self.base_url = base.rstrip("/")
        self.model = cfg.get("model", "gpt-4o-mini")
        self.temperature = float(cfg.get("temperature", 0.8))
        self.max_tokens = int(cfg.get("max_tokens", 800))
        self.timeout = float(cfg.get("request_timeout", 30))
        self.system_prompt = cfg.get(
            "system_prompt", "你是一个友好、简洁的QQ聊天助手。"
        )

    # ------------------------- 对外入口 -------------------------
    async def complete(self, messages: list, tools: list | None = None,
                       tool_choice: str = "auto") -> dict:
        """发起一次 Chat Completions 调用，返回原始响应 dict。

        支持 tools / tool_calls（由 agent 负责工具循环）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        try:
            raw = await asyncio.to_thread(self._post, payload)
        except Exception as exc:
            raise RuntimeError(f"调用 AI 接口失败: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"解析 AI 响应失败: {exc}; 原始: {raw[:300]}") from exc
        # 缓存命中日志（DeepSeek 在 usage 中返回对应字段）
        self._log_cache_usage(data.get("usage", {}))
        return data

    # ------------------------- 记忆压缩 -------------------------
    async def maybe_summarize(self, session_id, entry, mem_cfg, sent_messages):
        threshold = int(mem_cfg.get("summarize_threshold_tokens", 800000))
        keep = int(mem_cfg.get("keep_recent_turns", 200))
        approx_in = self.estimate_messages_tokens(sent_messages)
        # AND 触发：达到 token 阈值且近期原文超过 keep*2 才压缩。
        if approx_in < threshold or len(entry.get("recent", [])) <= keep * 2:
            return
        await self._summarize(session_id, entry, keep, mem_cfg)

    async def _summarize(self, session_id, entry, keep, mem_cfg):
        recent = entry.get("recent", [])
        if keep > 0:
            to_compress = recent[:-keep * 2]
            remaining = recent[-keep * 2:]
        else:
            to_compress = recent[:]
            remaining = []
        if not to_compress:
            return

        old_summary = entry.get("summary", "") or ""
        dialogue = "\n".join(f"{m['role']}: {m['content']}" for m in to_compress)
        prompt = (
            "请将下面的对话内容压缩为一段简洁、结构化的长期记忆摘要（中文）。"
            "保留：关键事实、用户偏好、双方约定、未完成的议题、重要人名/事件。"
            "如果提供了【旧摘要】，请将其与新材料合并去重，切勿丢失旧摘要中的重要信息。"
            "只输出摘要正文，不要任何解释或前后缀。\n\n"
            f"【旧摘要】\n{old_summary or '（无）'}\n\n"
            f"【待压缩的对话】\n{dialogue}"
        )
        try:
            raw = await asyncio.to_thread(
                self._post,
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是记忆压缩助手。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2000,
                },
            )
            data = json.loads(raw)
            new_summary = (data["choices"][0]["message"]["content"] or "").strip()
            if new_summary:
                entry["summary"] = new_summary
                entry["recent"] = remaining
                logger.info(
                    "会话 %s 已摘要压缩：压缩 %d 条，保留最近 %d 条，摘要 %d 字",
                    session_id, len(to_compress), len(remaining), len(new_summary),
                )
        except Exception as exc:
            logger.warning("摘要压缩失败（不影响本次回复）: %s", exc)

    # ------------------------- 缓存日志 -------------------------
    @staticmethod
    def _log_cache_usage(usage):
        if not isinstance(usage, dict):
            return
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if hit is None and miss is None:
            details = usage.get("prompt_tokens_details") or {}
            hit = details.get("cached_tokens")
        if hit is None and miss is None:
            return
        logger.info(
            "缓存[输入] 命中 %s / 未命中 %s tokens（合计约 %s）",
            hit or 0, miss or 0, usage.get("prompt_tokens", "?"),
        )

    # ------------------------- 工具 -------------------------
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """粗略估算 token：CJK 约 1 token/字，其余约 0.3 token/字。"""
        if not text:
            return 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return int(cjk + (len(text) - cjk) * 0.3)

    @staticmethod
    def estimate_messages_tokens(messages) -> int:
        return sum(LLMClient.estimate_tokens(m.get("content", "")) for m in messages)

    def _post(self, payload: dict) -> str:
        """同步发起 POST 请求，由 asyncio.to_thread 在后台线程执行。"""
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")
