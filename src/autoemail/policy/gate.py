"""发送闸门：所有出站消息的唯一出口（V0.2 §7）。

顺序固定，首个失败即降级。notices 只作提示，不影响 action。
"""

from dataclasses import dataclass, field

from .money import MANUAL, QuoteEval

AUTO_SEND = "auto_send"
DRAFT = "draft"
BLOCK = "block"

# 频控：同线程 24h 内最多自动发送次数（支撑正常协商多次来回，同时防刷屏）
MAX_AUTO_SENDS_PER_THREAD_24H = 10

# 证件类附件出站必须人工确认（模型侧已按 §9 放开，出站仍需人眼）
SENSITIVE_DOC_TYPES = frozenset({"driver_license", "passport", "state_id", "ssn_card"})


@dataclass
class GateContext:
    case_state: str
    auto_mode: str = "off"                  # off / dry_run / on
    manual_approval: bool = False
    human_owned: bool = False
    thread_frozen: bool = False

    assignment_auto_eligible: bool = True
    recipients: list[str] = field(default_factory=list)
    thread_participants: list[str] = field(default_factory=list)
    agent_domains: list[str] = field(default_factory=list)

    body_text: str = ""
    attachment_doc_types: list[str] = field(default_factory=list)
    other_case_markers: list[str] = field(default_factory=list)

    adcvd_statement_present: bool = False
    adcvd_attestation_present: bool = False
    redline_hits: list[str] = field(default_factory=list)

    quote: QuoteEval | None = None

    requires_broker_for_customs: bool = False
    commission_customs_broker: str | None = None
    agent_name: str = ""

    state_allows_auto: bool = True
    auto_sends_24h_in_thread: int = 0
    auto_sends_1h_to_agent: int = 0
    consecutive_failures: int = 0
    ledger_claimed: bool = True

    # 自动发送时间窗（纽约时间 09:00 起）；人工批准不受限
    auto_send_window_open: bool = True
    auto_send_window_desc: str = ""

    # 全局邮箱黑名单（DSN 退信拒收的地址）：命中即一律拦截，含人工发送
    blocked_recipients: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def evaluate(ctx: GateContext) -> Decision:
    notices = list(ctx.quote.notices) if ctx.quote else []

    def stop(action: str, rule_id: str, reason: str) -> Decision:
        return Decision(action, [reason], [rule_id], notices)

    # R13 全局黑名单：收件地址曾退信拒收，任何 case、任何模式（含人工）一律不发
    if ctx.blocked_recipients:
        return stop(BLOCK, "R13", f"收件人因退信拒收已进全局黑名单：{ctx.blocked_recipients}")

    # R01 人工接管 / 会话冻结 / 自动模式关闭
    if ctx.thread_frozen:
        return stop(BLOCK, "R01", "会话已冻结（疑似串户），禁止任何自动回复")
    if ctx.human_owned:
        return stop(BLOCK, "R01", "会话由人工接管中，自动化保持静默")
    if ctx.auto_mode == "off" and not ctx.manual_approval:
        return stop(DRAFT, "R01", "该 case 自动模式关闭，仅出草稿")

    # R02 归属不可靠
    if not ctx.assignment_auto_eligible:
        return stop(DRAFT, "R02", "归属置信度不足，只能人工确认后发送")

    # R03 收件人校验（防改址/防钓鱼）
    recipients = {addr.lower() for addr in ctx.recipients}
    allowed = {addr.lower() for addr in ctx.thread_participants}
    domains = {d.lower() for d in ctx.agent_domains}
    if not recipients <= allowed:
        return stop(BLOCK, "R03", f"收件人不在本线程参与者内：{sorted(recipients - allowed)}")
    for addr in recipients:
        domain = addr.split("@")[-1] if "@" in addr else ""
        if domains and domain not in domains:
            return stop(BLOCK, "R03", f"收件域名 {domain} 不属于代理商档案")

    # R04 跨案件隔离
    lowered = ctx.body_text.lower()
    leaked = [m for m in ctx.other_case_markers if m and m.lower() in lowered]
    if leaked:
        return stop(BLOCK, "R04", f"正文出现其他委托的标识：{leaked}")

    # R05 证件类附件
    sensitive = sorted(SENSITIVE_DOC_TYPES & set(ctx.attachment_doc_types))
    if sensitive:
        return stop(DRAFT, "R05", f"含证件类附件（{sensitive}），需人工确认")

    # R06 红线词 / AD/CVD 声明
    if ctx.redline_hits:
        return stop(DRAFT, "R06", f"命中红线词：{ctx.redline_hits}")
    if ctx.adcvd_statement_present and not ctx.adcvd_attestation_present:
        return stop(DRAFT, "R06", "AD/CVD 声明缺少人工签署记录，禁止自动发送")

    # R07 金额：自动发送不得越过审批线；人工批准视为已完成审批
    if ctx.quote and ctx.quote.action == MANUAL and not ctx.manual_approval:
        return stop(
            DRAFT,
            "R07",
            f"首年合计 {ctx.quote.total_first_year} 超过审批线，需人工审批",
        )

    # R08 强制报关行条款
    if ctx.requires_broker_for_customs:
        broker = (ctx.commission_customs_broker or "").strip()
        if not broker:
            return stop(DRAFT, "R08", "该代理商要求以其为报关行，但委托清关安排未知")
        if broker.lower() != ctx.agent_name.lower():
            return stop(
                BLOCK,
                "R08",
                f"代理条款要求以其为报关行，但当前清关行为 {broker}，接受将导致 Bond 被撤销",
            )

    # R09 状态机是否允许自动执行
    if not ctx.state_allows_auto:
        return stop(DRAFT, "R09", "当前状态不允许自动发送")

    # R10 频控与熔断：只约束自动发送，人工批准由人决定
    if not ctx.manual_approval:
        if ctx.consecutive_failures >= 2:
            return stop(DRAFT, "R10", "该线程连续发送失败，已熔断")
        if ctx.auto_sends_24h_in_thread >= MAX_AUTO_SENDS_PER_THREAD_24H:
            return stop(DRAFT, "R10", f"同线程 24 小时内自动发送已达 {MAX_AUTO_SENDS_PER_THREAD_24H} 封上限")
        if ctx.auto_sends_1h_to_agent >= 3:
            return stop(DRAFT, "R10", "对该代理商 1 小时内自动发送已达上限")

    # R11 时间窗：自动邮件从纽约时间每天 09:00 起（人工批准由人决定，不受限）
    if not ctx.manual_approval and not ctx.auto_send_window_open:
        return stop(
            DRAFT,
            "R11",
            f"未到自动发送时间窗（{ctx.auto_send_window_desc}），草稿待发送",
        )

    # R12 幂等抢占
    if not ctx.ledger_claimed:
        return stop(BLOCK, "R12", "发送台账未抢占成功，疑似重复发送")

    return Decision(AUTO_SEND, [], [], notices)
