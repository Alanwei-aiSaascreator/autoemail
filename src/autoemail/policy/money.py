"""金额规则（V0.2 §7.2）。

业务已明确：≤ $1,000 直接接受，> $1,000 才需考虑。
异常价只出提示，绝不改变动作——提示不能变成隐性审批线。
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

AUTO_ACCEPT = "auto_accept"
MANUAL = "manual"

_CENT = Decimal("0.01")


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class QuoteEval:
    total_first_year: Decimal
    currency: str
    action: str
    notices: list[str] = field(default_factory=list)
    vs_history_ratio: Decimal | None = None


def evaluate_quote(
    amount: object,
    *,
    currency: str = "USD",
    extras: tuple | list = (),
    usd_rate: object | None = None,
    history_avg: object | None = None,
    approval_line: object = Decimal("1000"),
    anomaly_ratio: object = Decimal("1.3"),
) -> QuoteEval:
    """计算首年现金总支出并给出动作。

    extras 为必收附加费（如 Aldenbridge 的 CF5106 $55），必须计入判定。
    """
    code = (currency or "USD").upper()
    total = _dec(amount) + sum((_dec(item) for item in extras), Decimal("0"))
    notices: list[str] = []

    if code != "USD":
        if usd_rate is None:
            return QuoteEval(
                total_first_year=total.quantize(_CENT, ROUND_HALF_UP),
                currency=code,
                action=MANUAL,
                notices=[f"报价币种为 {code} 且未提供汇率，需人工折算后判定"],
            )
        converted = (total * _dec(usd_rate)).quantize(_CENT, ROUND_HALF_UP)
        notices.append(f"按汇率 {usd_rate} 折算：{total} {code} → {converted} USD")
        total = converted
    else:
        total = total.quantize(_CENT, ROUND_HALF_UP)

    line = _dec(approval_line)
    action = AUTO_ACCEPT if total <= line else MANUAL

    ratio: Decimal | None = None
    if history_avg is not None and _dec(history_avg) > 0:
        ratio = (total / _dec(history_avg)).quantize(Decimal("0.001"))
        if ratio > _dec(anomaly_ratio):
            notices.append(
                f"异常价提示：首年合计 {total} 为历史均价 {history_avg} 的 {ratio} 倍"
                "（仅提示，不改变动作）"
            )

    return QuoteEval(
        total_first_year=total,
        currency="USD" if code != "USD" else code,
        action=action,
        notices=notices,
        vs_history_ratio=ratio,
    )
