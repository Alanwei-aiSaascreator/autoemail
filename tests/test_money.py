from decimal import Decimal

from autoemail.policy.money import AUTO_ACCEPT, MANUAL, evaluate_quote


def test_below_line_auto_accepts():
    result = evaluate_quote(750)
    assert result.action == AUTO_ACCEPT
    assert result.total_first_year == Decimal("750.00")


def test_exactly_at_line_auto_accepts():
    assert evaluate_quote(1000).action == AUTO_ACCEPT


def test_above_line_goes_manual():
    assert evaluate_quote(Decimal("1000.01")).action == MANUAL


def test_required_extras_counted():
    # Aldenbridge：$690 + CF5106 $55 = $745，仍在审批线内
    result = evaluate_quote(690, extras=[55])
    assert result.total_first_year == Decimal("745.00")
    assert result.action == AUTO_ACCEPT


def test_extras_can_push_over_line():
    result = evaluate_quote(980, extras=[55])
    assert result.total_first_year == Decimal("1035.00")
    assert result.action == MANUAL


def test_anomaly_price_only_notices():
    # 高于历史均价 80%，但仍在审批线内：只提示，不改变动作
    result = evaluate_quote(900, history_avg=500)
    assert result.action == AUTO_ACCEPT
    assert result.vs_history_ratio == Decimal("1.800")
    assert any("异常价提示" in note for note in result.notices)


def test_non_usd_without_rate_goes_manual():
    result = evaluate_quote(800, currency="CAD")
    assert result.action == MANUAL
    assert any("汇率" in note for note in result.notices)


def test_non_usd_with_rate_converts():
    result = evaluate_quote(800, currency="CAD", usd_rate="0.73")
    assert result.total_first_year == Decimal("584.00")
    assert result.action == AUTO_ACCEPT
