"""文本归一化工具：归属匹配与线程主题归一都依赖它。"""

import re
import unicodedata

# 保留字母数字与 CJK，其余视为分隔符
_NON_WORD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|回复|转发)\s*[:：]\s*)+", re.IGNORECASE)


def normalize_text(value: str) -> str:
    """大写、去标点、压缩空格，用于法定名/公司名匹配。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).upper()
    return _NON_WORD.sub(" ", text).strip()


def normalize_subject(value: str) -> str:
    """去掉 Re:/Fwd: 前缀并归一，用于识别"同一主题的另一条线程"。"""
    if not value:
        return ""
    text = _SUBJECT_PREFIX.sub("", value)
    return re.sub(r"\s+", " ", text).strip().lower()
