"""媒体适配层：把 NapCat 发来的图片 / 语音 / 文件，解析成本地可处理路径。

NapCat 通过 OneBot v11 把媒体作为消息段（image / record）或通知事件
（group_upload / offline_file）下发。段里通常带一个 `url`（可直接下载）或一个
`file` 文件 id（需再调 NapCat API 换取本机路径）。本模块优先用 `url`，
缺失时回退到 API，并把结果落盘到本地缓存目录，方便后续视觉/ASR/转发。
"""
import asyncio
import logging
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger("QQAIBot.Media")

_DOWNLOAD_DIR = Path(__file__).parent / "downloads"

# 单个媒体文件下载上限。流式写盘后内存只占一个分块，这个限制是为了保护磁盘与等待时长；
# 群文件/QQ 视频都走这里，超限直接报错而不是把进程拖垮。
_MAX_DOWNLOAD_MB = 120
_CHUNK = 256 * 1024


def _download(url: str, suffix: str = ".bin", max_mb: int = 0) -> str:
    """下载到 downloads/ 并返回本地路径（流式写盘，不把整个文件读进内存）。

    **同步函数**：内部是 urllib 阻塞 IO。调用方必须用 `asyncio.to_thread(_download, ...)`，
    否则会在协程里阻塞整个事件循环（包括 WS 读循环），表现为心跳超时、其它请求全部 10s 超时。

    max_mb 为 0 时用模块默认值 `_MAX_DOWNLOAD_MB`。
    先看 Content-Length 提前拒绝，再边读边写、超限即中止，避免大文件撑爆内存/磁盘。
    """
    _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    limit = (max_mb or _MAX_DOWNLOAD_MB) * 1024 * 1024
    out = _DOWNLOAD_DIR / f"m_{uuid.uuid4().hex}{suffix}"
    tmp = out.with_name(out.name + ".part")
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0")
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ValueError(
                    f"文件过大（{int(declared) / 1048576:.1f}MB），单文件上限 {limit // 1048576}MB")
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"文件超过 {limit // 1048576}MB 上限，已中止下载")
                    f.write(chunk)
        tmp.replace(out)
        return str(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        raise


async def _from_api_data(data: dict, fallback_suffix: str, action: str,
                         prefer_url: bool = True) -> str | None:
    """把 NapCat 文件接口返回的 data 转成 bot 可读的本地路径。

    为什么要优先用 url 自己下载：bot 与 NapCat 可能分处不同容器，data.file 是
    NapCat 视角的路径（如 /app/.config/QQ/...），bot 侧 Path.exists() 往往为假。
    url 则是统一的下载入口。

    例外是语音：get_record 已按 out_format=mp3 在 NapCat 侧转码，data.file 才是
    mp3；data.url 可能是未处理的 raw silk。所以语音用 prefer_url=False，先取本地
    文件，不可读时再退回 url（交给下游 _to_mp3 兜底）。
    """
    if not isinstance(data, dict):
        return None
    suffix = Path(str(data.get("file_name") or "")).suffix or fallback_suffix

    async def _by_url() -> str | None:
        url = data.get("url")
        if not url:
            return None
        try:
            return await asyncio.to_thread(_download, url, suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 返回的 url 下载失败: %s", action, exc)
            return None

    def _by_file() -> str | None:
        p = data.get("file")
        if p and Path(p).exists():
            return p
        if p:
            logger.warning("%s 返回的本地路径在 bot 侧不可读（可能跨容器）：%s",
                           action, p)
        return None

    if prefer_url:
        return await _by_url() or _by_file()
    return _by_file() or await _by_url()


async def fetch_image(seg_data: dict, call_api) -> str | None:
    """从 image 段取本地图片路径。返回 None 表示无法获取。"""
    if not isinstance(seg_data, dict):
        return None
    url = seg_data.get("url")
    if url:
        try:
            # 按真实后缀落盘：_ingest_media 靠扩展名判断是否需要 ffmpeg 转码，
            # 一律存成 .png 会把 JPEG 谎报成 PNG、把 ogg/amr 谎报成 mp3，导致方舟 400。
            suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
            return await asyncio.to_thread(_download, url, suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载图片失败，尝试 API: %s", exc)
    fid = seg_data.get("file")
    if fid and call_api:
        try:
            # 官方定义：get_image 的 file / file_id 二选一即可，此处用 file_id。
            resp = await call_api("get_image", {"file_id": fid})
            if resp and resp.get("status") == "ok":
                return await _from_api_data(resp.get("data") or {}, ".png", "get_image")
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_image 失败: %s", exc)
    return None


async def fetch_voice(seg_data: dict, call_api) -> str | None:
    """从 record 段取本地语音路径（尽量转 mp3 便于 ASR）。"""
    if not isinstance(seg_data, dict):
        return None
    url = seg_data.get("url")
    if url:
        try:
            # 同上：QQ 语音实际多为 amr/silk/ogg，存成 .mp3 会让转码分支永远不触发
            suffix = Path(urllib.parse.urlparse(url).path).suffix or ".mp3"
            return await asyncio.to_thread(_download, url, suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载语音失败，尝试 API: %s", exc)
    fid = seg_data.get("file")
    if fid and call_api:
        try:
            # 官方定义：file_id 可选、out_format 必填。转码后的 mp3 在 data.file，
            # data.url 可能是未处理的 raw silk，所以这里 prefer_url=False。
            resp = await call_api("get_record", {"file_id": fid, "out_format": "mp3"})
            if resp and resp.get("status") == "ok":
                return await _from_api_data(
                    resp.get("data") or {}, ".mp3", "get_record", prefer_url=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_record 失败: %s", exc)
    return None


async def fetch_video(seg_data: dict, call_api) -> str | None:
    """从 video 段取本地视频路径（优先下载 url，缺失时用 get_file API 换取本机路径）。"""
    if not isinstance(seg_data, dict):
        return None
    url = seg_data.get("url")
    if url:
        try:
            suffix = Path(urllib.parse.urlparse(url).path).suffix or ".mp4"
            return await asyncio.to_thread(_download, url, suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载视频失败，尝试 API: %s", exc)
    fid = seg_data.get("file")
    if fid and call_api:
        try:
            # NapCat 没有 get_video（官方接口只有 get_image / get_record / get_file），
            # 视频统一走 get_file，它同样接受 file_id。
            resp = await call_api("get_file", {"file_id": fid})
            if resp and resp.get("status") == "ok":
                return await _from_api_data(resp.get("data") or {}, ".mp4", "get_file")
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_file 失败: %s", exc)
    return None


async def fetch_file(notice: dict, msg_type: str, call_api) -> tuple[str, str] | None:
    """从文件通知事件取 (文件名, 本地路径)。

    接口链与 `OneBotClient._resolve_file_seg` 保持一致，全部按 NapCat 官方定义：
      私聊：get_private_file_url（file_id 必填，返回 data.url）
            → get_file（file_id，返回 data.file / data.url）
      群聊：get_group_file_url（group_id + file_id + busid，返回 data.url）

    注意：NapCat **没有** `get_offline_file`（那是 go-cqhttp 的接口），调用只会拿到
    retcode 1404，所以这里不能再用它兜底。
    """
    if not isinstance(notice, dict):
        return None
    name = notice.get("file_name") or f"file_{uuid.uuid4().hex}"
    suffix = Path(name).suffix or ".bin"

    # 私聊离线文件可能直接带下载地址
    direct = notice.get("file_url") or notice.get("url")
    if direct:
        try:
            return name, await asyncio.to_thread(_download, direct, suffix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载离线文件失败: %s", exc)

    fid = notice.get("file_id") or notice.get("id")
    if not fid or not call_api:
        return None

    if msg_type == "group":
        attempts = [(
            "get_group_file_url",
            {"group_id": int(notice.get("group_id") or 0),
             "file_id": fid, "busid": int(notice.get("busid") or 0)},
        )]
    else:
        priv = {"file_id": fid}
        # 官方虽只要求 file_id，但示例带 user_id；有就一并给，避免某些版本按私聊会话定位。
        uid = notice.get("user_id")
        if uid:
            try:
                priv["user_id"] = int(uid)
            except (TypeError, ValueError):
                pass
        attempts = [
            ("get_private_file_url", priv),
            ("get_file", {"file_id": fid}),
        ]

    for action, params in attempts:
        try:
            resp = await call_api(action, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 调用失败: %s", action, exc)
            continue
        if isinstance(resp, dict) and resp.get("status") == "ok":
            path = await _from_api_data(resp.get("data") or {}, suffix, action)
            if path:
                return name, path
        else:
            logger.debug("%s 未返回可用文件: %s", action, str(resp)[:120])

    logger.warning("无法获取文件：%s（file_id=%s，尝试过 %s）",
                   name, fid, [a for a, _ in attempts])
    return None
