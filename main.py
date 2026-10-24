"""余额查询插件：让机器人知道自己还剩多少额度，并自带收款码。

能力：
  1. 查询 DeepSeek 账户余额（LLM 工具 get_api_balance + 命令 /余额）；
  2. 自带收款码：私聊发「/收款码」+ 图片即可保存，不再依赖别的插件；
  3. 收款码联动有两种模式（pay_mode）：
       - ask_model：低余额时提示模型可以发收款码，发不发由它决定；
       - auto_send：插件自己在后台判定，低于阈值就主动发，冷却时间限制频率。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 优先用官方 API 拿数据目录
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    DATA_ROOT = Path(get_astrbot_data_path())
except Exception:  # pragma: no cover - 老版本兜底：插件在 <data>/plugins/<name>/main.py
    DATA_ROOT = Path(__file__).resolve().parents[2]

BALANCE_API = "https://api.deepseek.com/user/balance"
PLUGIN_DATA = DATA_ROOT / "plugin_data" / "astrbot_plugin_apibalance"
STATE_FILE = PLUGIN_DATA / "state.json"
QR_DIR = PLUGIN_DATA / "payment_qr"

MODE_ASK = "ask_model"
MODE_AUTO = "auto_send"

DEFAULT_HINT = (
    "【余额提醒】当前可用余额只有 {balance} 元，低于 {threshold} 元的提醒线。"
    "如果正好聊到请客、赞助、打钱之类的话题，你可以调用 send_my_payment_qr 工具把收款码发出去。"
)

DEFAULT_VAGUE_HINT = (
    "【余额提醒】大白饭快吃光了（具体金额只有管理员能看）。"
    "如果正好聊到请客、赞助、打钱，你可以调用 send_my_payment_qr 发收款码，"
    "并说「大白饭快吃光了」「你问我管理员去」这类话，别报具体数字。"
)

VIS_EVERYONE = "everyone"
VIS_ADMIN = "admin_only"

DEFAULT_QR_CAPTION = "大白饭快吃光了，求你给我打点米吧QwQ"


# ── 余额接口 ──────────────────────────────────────────────────────
def _read_deepseek_key(configured: str = "") -> str:
    """取 DeepSeek API Key：优先插件自带配置，其次读 AstrBot 的 provider 配置。"""
    if str(configured or "").strip():
        return str(configured).strip()
    try:
        cfg = json.loads((DATA_ROOT / "cmd_config.json").read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[apibalance] 读取配置失败: {exc}")
        return ""
    for src in cfg.get("provider_sources", []) or []:
        if str(src.get("provider", "")).lower() == "deepseek":
            keys = src.get("key") or []
            if keys:
                return str(keys[0])
    return ""


async def _fetch_balance(api_key: str = "") -> dict:
    """请求 DeepSeek 余额接口，返回 {ok, data} 或 {ok: False, error}。"""
    key = _read_deepseek_key(api_key)
    if not key:
        return {"ok": False, "error": "没有在 AstrBot 配置里找到 DeepSeek API Key"}
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BALANCE_API, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                return {"ok": True, "data": json.loads(body)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _total_balance(data: dict) -> float | None:
    """取第一条余额信息里的可用总额，取不到返回 None。"""
    infos = data.get("balance_infos") or []
    if not infos:
        return None
    try:
        return float(infos[0].get("total_balance"))
    except (TypeError, ValueError):
        return None


def _format(data: dict) -> str:
    """把接口返回格式化成一句人话。"""
    infos = data.get("balance_infos") or []
    if not infos:
        return "接口没有返回余额信息"
    lines = []
    for info in infos:
        lines.append(
            f"{info.get('currency', 'CNY')} 可用 {info.get('total_balance', '?')}"
            f"（充值 {info.get('topup_balance', info.get('topped_up_balance', '?'))}"
            f" / 赠送 {info.get('granted_balance', '?')}）"
        )
    state = "可用" if data.get("is_available") else "不可用（可能欠费）"
    return f"DeepSeek 账户状态：{state}\n" + "\n".join(lines)


# ── 收款码存取 ────────────────────────────────────────────────────
def _sniff_image_ext(data: bytes) -> str | None:
    """根据文件头判断图片格式，返回扩展名；不是图片返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def find_own_qr() -> Path | None:
    """返回插件自己保存的收款码文件（没有则 None）。"""
    try:
        for p in sorted(QR_DIR.glob("payment_qr.*")):
            if p.is_file() and p.stat().st_size > 0:
                return p
    except OSError:
        return None
    return None


@register("astrbot_plugin_dsbalance", "Mao19927", "DeepSeek 余额查询、收款码管理与低余额提醒", "1.0.0")
class ApiBalancePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        cfg = dict(config or {})
        self.mode = str(cfg.get("pay_mode") or MODE_ASK)
        try:
            self.threshold = float(cfg.get("low_balance_threshold", 5.0) or 0)
        except (TypeError, ValueError):
            self.threshold = 5.0
        try:
            self.cooldown = float(cfg.get("auto_send_cooldown_hours", 6.0) or 6.0) * 3600
        except (TypeError, ValueError):
            self.cooldown = 6 * 3600
        try:
            self.check_interval = float(cfg.get("auto_check_interval_minutes", 30) or 30) * 60
        except (TypeError, ValueError):
            self.check_interval = 30 * 60
        self.target_umo = str(cfg.get("auto_send_umo") or "").strip()
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.qr_files = [str(x) for x in (cfg.get("payment_qr") or []) if str(x).strip()]
        self.manual_qr = str(cfg.get("qr_image_path") or "").strip()
        self.enable_pay_hint = bool(cfg.get("enable_pay_hint", True))
        self.hint_text = str(cfg.get("hint_text") or DEFAULT_HINT)
        self.visibility = str(cfg.get("balance_visibility") or VIS_EVERYONE)
        self.vague_text = str(cfg.get("vague_low_text") or DEFAULT_VAGUE_HINT)
        self.qr_caption = str(cfg.get("qr_caption") or DEFAULT_QR_CAPTION)

        self._state = self._load_state()
        self._task: asyncio.Task | None = None
        if self.mode == MODE_AUTO and self.threshold > 0:
            try:
                self._task = asyncio.create_task(self._auto_loop())
                logger.info(
                    f"[apibalance] 自动发送已启用：阈值 {self.threshold} 元，"
                    f"冷却 {self.cooldown / 3600:.1f} 小时"
                )
            except RuntimeError as exc:  # 没有事件循环时静默跳过
                logger.warning(f"[apibalance] 后台任务启动失败: {exc}")

    # ── 状态 ────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[apibalance] 状态写入失败: {exc}")

    def _remember_session(self, event: AstrMessageEvent) -> None:
        """记住最近一次私聊会话，供后台自动发送使用。"""
        try:
            if event.is_private_chat():
                self._state["last_private_umo"] = event.unified_msg_origin
                self._save_state()
        except Exception:  # noqa: BLE001
            pass

    # ── 收款码 ──────────────────────────────────────────────────
    def _configured_qr(self) -> str | None:
        """解析面板里上传的收款码（AstrBot 把它们放在 plugin_data 下）。"""
        for raw in self.qr_files:
            for cand in (
                PLUGIN_DATA / raw,
                DATA_ROOT / raw,
                Path(raw).expanduser(),
                DATA_ROOT.parent / raw,
            ):
                try:
                    if cand.is_file():
                        return str(cand.resolve())
                except OSError:
                    continue
        return None

    def _resolve_qr_path(self) -> str | None:
        """收款码查找顺序：面板上传 → 命令保存 → 手填路径 → 兼容旧 payqr 配置。"""
        configured = self._configured_qr()
        if configured:
            return configured
        own = find_own_qr()
        if own:
            return str(own.resolve())
        if self.manual_qr:
            p = Path(self.manual_qr).expanduser()
            if p.is_file():
                return str(p.resolve())
        try:  # 兼容：如果还装着 payqr，读它的配置当兜底
            cfg = json.loads(
                (DATA_ROOT / "config" / "astrbot_plugin_payqr_config.json").read_text(encoding="utf-8-sig")
            )
            files = cfg.get("payment_qr") or []
            if files:
                raw = str(files[0])
                for cand in (
                    Path(raw),
                    DATA_ROOT / "plugin_data" / "astrbot_plugin_payqr" / raw,
                    DATA_ROOT / raw,
                ):
                    if cand.is_file():
                        return str(cand.resolve())
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _save_qr_from_image(self, img: Image) -> Path | None:
        """把收到的图片存成收款码，返回保存后的路径。"""
        try:
            src = await img.convert_to_file_path()
            data = Path(src).read_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[apibalance] 读取图片失败: {exc}")
            return None
        ext = _sniff_image_ext(data)
        if not ext:
            logger.warning("[apibalance] 收到的文件不是可识别的图片")
            return None
        try:
            QR_DIR.mkdir(parents=True, exist_ok=True)
            for old in QR_DIR.glob("payment_qr.*"):
                old.unlink(missing_ok=True)
            target = QR_DIR / f"payment_qr{ext}"
            target.write_bytes(data)
            logger.info(f"[apibalance] 收款码已保存: {target} ({len(data)} 字节)")
            return target
        except OSError as exc:
            logger.error(f"[apibalance] 保存收款码失败: {exc}")
            return None

    def _clear_qr(self) -> bool:
        removed = False
        for old in QR_DIR.glob("payment_qr.*"):
            try:
                old.unlink()
                removed = True
            except OSError:
                pass
        return removed

    # ── 判定与发送 ──────────────────────────────────────────────
    def _cooldown_ready(self) -> bool:
        last = float(self._state.get("last_auto_send_ts", 0) or 0)
        return (time.time() - last) >= self.cooldown

    def _maybe_hint(self, data: dict, is_admin: bool = True) -> str:
        """ask_model 模式下，余额低于阈值时返回提示文本。

        非管理员且设置为「仅管理员可见」时，只给不含数字的模糊提示。
        """
        if not self.enable_pay_hint or self.threshold <= 0:
            return ""
        balance = _total_balance(data)
        if balance is None or balance >= self.threshold:
            return ""
        if is_admin or self.visibility == VIS_EVERYONE:
            try:
                return "\n\n" + self.hint_text.format(balance=balance, threshold=self.threshold)
            except (KeyError, ValueError):
                return "\n\n" + DEFAULT_HINT.format(balance=balance, threshold=self.threshold)
        return "\n\n" + self.vague_text

    async def _send_qr(self, umo: str, balance: float | None = None) -> bool:
        """发送收款码到指定会话；成功返回 True。

        说明文案里不带具体金额——收款码可能发给非管理员，余额不该跟着泄露。
        """
        qr = self._resolve_qr_path()
        if not qr:
            logger.warning("[apibalance] 还没有收款码，先私聊发「/收款码」+ 图片")
            return False
        caption = self.qr_caption
        try:
            chain = MessageChain().message(caption).file_image(qr)
            await self.context.send_message(umo, chain)
            self._state["last_auto_send_ts"] = time.time()
            self._save_state()
            logger.info(f"[apibalance] 已发送收款码 -> {umo}（余额 {balance}）")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[apibalance] 发送收款码失败: {exc}")
            return False

    async def _auto_loop(self) -> None:
        """auto_send 模式的后台定时检查。"""
        await asyncio.sleep(min(self.check_interval, 120))
        while True:
            try:
                await self._auto_check()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[apibalance] 自动检查异常: {exc}")
            await asyncio.sleep(self.check_interval)

    async def _auto_check(self) -> None:
        res = await _fetch_balance(self.api_key)
        if not res["ok"]:
            return
        balance = _total_balance(res["data"])
        if balance is None or balance >= self.threshold:
            return
        if not self._cooldown_ready():
            return
        umo = self.target_umo or str(self._state.get("last_private_umo") or "")
        if not umo:
            logger.info("[apibalance] 余额偏低，但还没有可发送的私聊会话")
            return
        await self._send_qr(umo, balance)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    # ── 命令与工具 ──────────────────────────────────────────────
    @filter.command("收款码")
    async def qr_cmd(self, event: AstrMessageEvent):
        """发图保存收款码（仅管理员）；不带图则回显当前收款码；带「清除」则删除（仅管理员）。"""
        text = (event.message_str or "").strip()
        images = [
            comp
            for comp in getattr(event.message_obj, "message", []) or []
            if isinstance(comp, Image)
        ]
        wants_change = bool(images) or ("清除" in text) or ("删除" in text)

        # 改码属于敏感操作：任何人都能换掉收款码，等于能把钱导到别处
        if wants_change and not event.is_admin():
            logger.warning(
                f"[apibalance] 非管理员尝试修改收款码: {event.get_sender_id()} @ {event.unified_msg_origin}"
            )
            event.stop_event()
            yield event.plain_result("收款码只有管理员能改。")
            return

        if images:
            saved = await self._save_qr_from_image(images[0])
            if saved:
                yield event.plain_result(f"收款码已保存（{saved.name}），以后需要时会发这张。")
            else:
                yield event.plain_result("保存失败：这张图没识别成常见格式（png/jpg/gif/webp），换一张试试。")
            return

        if "清除" in text or "删除" in text:
            yield event.plain_result("已删除收款码。" if self._clear_qr() else "本来就没有存过收款码。")
            return

        qr = self._resolve_qr_path()
        if not qr:
            yield event.plain_result("还没有收款码：私聊发「/收款码」并附一张图即可。")
            return
        yield event.chain_result([Plain("这是我现在的收款码："), Image.fromFileSystem(qr)])

    @filter.command("余额")
    async def balance_cmd(self, event: AstrMessageEvent):
        """查询当前 DeepSeek 账户剩余额度（是否对所有人公开由配置决定）。"""
        self._remember_session(event)
        is_admin = event.is_admin()
        res = await _fetch_balance(self.api_key)
        if not res["ok"]:
            yield event.plain_result(f"查询失败：{res['error']}")
            return
        data = res["data"]
        balance = _total_balance(data)
        low = balance is not None and self.threshold > 0 and balance < self.threshold
        detailed = is_admin or self.visibility == VIS_EVERYONE

        if self.mode == MODE_AUTO and low and self._cooldown_ready():
            sent = await self._send_qr(event.unified_msg_origin, balance)
            if detailed:
                yield event.plain_result(
                    _format(data)
                    + ("\n\n（已在冷却期外，收款码已发送）" if sent else "\n\n（收款码发送失败：还没设置收款码）")
                )
            else:
                yield event.plain_result(self.vague_text)
            return

        if not detailed:
            yield event.plain_result(self.vague_text if low else "余额还够吃一阵子（具体数字只有管理员能看）。")
            return

        text = _format(data)
        if self.mode == MODE_ASK and self._maybe_hint(data, is_admin):
            text += "\n（余额偏低，可以让我发收款码）"
        yield event.plain_result(text)

    @llm_tool(name="get_api_balance")
    async def get_api_balance(self, event: AstrMessageEvent) -> str:
        """查询机器人当前使用的 DeepSeek 账户还剩多少额度。

        当用户问「你还有多少钱」「额度还剩多少」「还能聊多久」这类问题时调用。
        如果结果是模糊提示（不含数字），说明当前用户不该看到具体金额，
        你要用「大白饭快吃光了」「你问我管理员去」这类话回答，不要编造数字。

        Returns:
            str: 账户余额说明；余额偏低时可能附带提醒；无权限时只返回模糊提示。
        """
        self._remember_session(event)
        is_admin = event.is_admin()
        res = await _fetch_balance(self.api_key)
        if not res["ok"]:
            return f"查询失败：{res['error']}"
        data = res["data"]
        balance = _total_balance(data)
        low = balance is not None and self.threshold > 0 and balance < self.threshold
        detailed = is_admin or self.visibility == VIS_EVERYONE

        if self.mode == MODE_AUTO and low and self._cooldown_ready():
            sent = await self._send_qr(event.unified_msg_origin, balance)
            tail = "已把收款码发给对方。" if sent else "收款码发送失败，请提醒管理员先设置收款码。"
            if detailed:
                return _format(data) + f"\n\n【已触发自动发送】{tail}"
            return self.vague_text + f"\n\n【已触发自动发送】{tail}"

        if not detailed:
            return self.vague_text if low else "余额还够吃一阵子（具体数字只有管理员能看）。"

        return _format(data) + self._maybe_hint(data, is_admin)

    @llm_tool(name="send_my_payment_qr")
    async def send_my_payment_qr(self, event: AstrMessageEvent) -> str:
        """把收款码图片发给当前对话。

        当你想跟对方要钱、要赞助、卖惨说自己没钱了、或者对方主动问怎么打钱时调用。
        不要频繁调用，一次对话最多一次。

        Returns:
            str: 发送结果。
        """
        qr = self._resolve_qr_path()
        if not qr:
            return "还没有设置收款码，先让管理员私聊发「/收款码」并附一张图。"
        try:
            chain = MessageChain().message(self.qr_caption).file_image(qr)
            await self.context.send_message(event.unified_msg_origin, chain)
            return "收款码图片已发送，可以继续正常回复。"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[apibalance] 发送收款码失败: {exc}")
            return f"发送收款码失败: {exc}"
