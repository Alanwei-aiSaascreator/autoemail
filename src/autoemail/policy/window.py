"""自动发送时间窗：自动邮件仅在美国纽约时间每天 09:00–17:00 允许发出。

只约束自动发送（auto path）；人工批准发送由人决定，不受时间窗限制。
时区与起止时间可在 config 调整（AUTOEMAIL_AUTO_SEND_TIMEZONE / _START_ET / _END_ET）。
之前只判断"09:00 起"没有结束边界，导致北京早上（纽约前一天晚上）也会触发批次，
已加上结束时间：过了结束时刻一律视为未开窗。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = (int(part) for part in value.split(":"))
    return hh, mm


def auto_send_window_open(now: datetime | None = None) -> tuple[bool, str]:
    """判断当前是否在自动发送时间窗内（含起点、不含终点）。

    返回 (是否开窗, 描述)；传入 now 仅用于测试，需带时区或为 UTC。
    """
    tz = ZoneInfo(settings.auto_send_timezone)
    local = (now or datetime.now(tz=tz)).astimezone(tz)
    start = _parse_hhmm(settings.auto_send_start_et)
    end = _parse_hhmm(settings.auto_send_end_et)
    current = (local.hour, local.minute)
    opened = start <= current < end
    desc = f"纽约时间每天 {settings.auto_send_start_et}–{settings.auto_send_end_et}"
    return opened, desc
