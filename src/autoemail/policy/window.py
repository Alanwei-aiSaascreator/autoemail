"""自动发送时间窗：自动邮件仅在美国纽约时间每天 09:00 起允许发出。

只约束自动发送（auto path）；人工批准发送由人决定，不受时间窗限制。
时区与起始时间可在 config 调整（AUTOEMAIL_AUTO_SEND_TIMEZONE / _START_ET）。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings


def auto_send_window_open(now: datetime | None = None) -> tuple[bool, str]:
    """判断当前是否已进入自动发送时间窗。

    返回 (是否已开始, 描述)；传入 now 仅用于测试，需带时区或为 UTC。
    """
    tz = ZoneInfo(settings.auto_send_timezone)
    local = (now or datetime.now(tz=tz)).astimezone(tz)
    hh, mm = (int(part) for part in settings.auto_send_start_et.split(":"))
    opened = (local.hour, local.minute) >= (hh, mm)
    desc = f"纽约时间每天 {settings.auto_send_start_et} 起"
    return opened, desc
