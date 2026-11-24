# autoemail

Bond 采购业务邮件自动化 Agent（公司内部使用）。一期只处理与 Bond 代理商之间的邮件往来。

技术方案见 `docs/` 外的 `Bond邮件Agent技术方案V0.2.md`（当前在需求工作区）。

## 当前进度：M0 骨架

已实现：

- 数据模型（`src/autoemail/models.py`）：委托 / 代理商 / case / 线程 / 消息 / 附件 / 报价 / Invoice / 草稿 / 审计 / 发送台账
- 渠道层：`EmlDirectoryChannel`（.eml 回放，用于历史邮件与测试）、`ImapSmtpChannel`（IMAP 收 + SMTP 发，已实测真实收发）
- 入站链路：RFC822 解析 → 去重 → 线程还原 → 归属解析（六级阶梯）
- 意图识别：确定性识别报价、申请材料、Invoice、Bond 文件；优先提取付款信息
- 报价入库与金额判定：按首年现金总支出（含代理商附加费）入库，≤ $1,000 自动接受，> $1,000 进入人工审批；异常价只提示不阻断
- LLM/RAG：默认关闭；开启后仅对 unclear 意图做受控 fallback（不推进状态、不自动发送），可为草稿提供建议正文，RAG 按 case 隔离检索内部知识
- 付款闭环：正文 + 发票 PDF 文本提取金额/到期日/付款方式/发票号 → 按发票号去重写入 Invoice → 推送企业微信群安排支付 → 人工回填付款方式与流水号，case 进入 paid
- 主动询价：`initiate` 创建委托×代理商 case，生成带 case token 的初始询价邮件；dry_run 进入审核队列，on 模式发送
- 每日随机批量询价：文件齐全的委托每天自动随机选 20 家代理商发询价（同委托同代理商永不重复，多委托全局去重）；委托付款后自动停止并关闭其余案件；报价即推飞书，人工对比低者得、付款人工执行；`inquiry-batch` 命令可手动触发
- 委托文件管理：询价前必须上传公司章程 / 税号文件 / 法人证件三份文件，缺一不放行；审核台可上传与新建委托
- 一键询价：审核台「发起询价」页录入委托 + 选代理商 → 校验资料与文件齐全 → 生成询价草稿（或直接发送）
- 守护模式：`mail watch` 持续轮询收件箱，自动识别回复、过闸门并自动回复；认证失败不退出，等待凭据恢复后自动重连
- 自动模式管理：CLI 与审核台均可切换 off / dry_run / on，变更写入审计
- 设置页：浏览器内录入或更新 IMAP/SMTP 授权码与企业微信 webhook，保存后写入本地 .env 并立即生效，密钥不回显
- 评测集与回归：`eval export` 导出入站邮件标注文件（predicted 预填），人工修订 expected 后用 `eval run` 计算意图、归属 Case、归属方式的准确率与不一致明细
- 历史邮件批量导入：`import` 按时间顺序回放目录或单个 .eml，默认只出草稿、不自动发送，重复导入按去重哈希跳过
- 状态推进与催办：报价/材料请求/Bond 文件回复推进 case；`followup scan/run` 扫描到期催办并生成审核草稿
- 草稿审核：`draft list/show/edit/approve/reject`，批准时重新过发送闸门并记录出站结果
- 规则层：金额规则、发送闸门（11 条检查，顺序固定）；跨委托隔离、AD/CVD 签署校验、报关行冲突、频控与熔断均已接入真实数据
- 通知层：企业微信群机器人（付款信息推送）

未实现（M1 后续）：填表（PDF 申请表填写与附件生成）、多轮议价、Alembic 迁移、规则配置表。本轮 Invoice 新增发票号与付款回填列、委托文件表，已有本地 sqlite 库需用 `init-db` 重建或手工 ALTER。

## LLM 与 RAG（默认全关）

所有开关都在 `.env` 中用 `AUTOEMAIL_` 前缀显式打开，不配置时行为与纯确定性流程完全一致。

| 配置 | 作用 |
| --- | --- |
| `AUTOEMAIL_LLM_ENABLED` | 允许按 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 创建 OpenAI 兼容 provider |
| `AUTOEMAIL_LLM_INTENT_FALLBACK_ENABLED` | 仅在确定性识别为 `unclear` 时调用模型兜底 |
| `AUTOEMAIL_LLM_SHADOW_MODE` | 确定性识别成功时也调用模型，只记录 `agree/disagree` 审计，不影响结果 |
| `AUTOEMAIL_LLM_DRAFT_ENABLED` | 允许模型生成草稿建议；失败或校验不通过自动回退模板 |
| `AUTOEMAIL_RAG_ENABLED` | 允许用 `InMemoryRetriever` 按 case 召回内部知识，文档 ID 写入草稿 `controlled_fields` |
| `AUTOEMAIL_LLM_AUTO_SEND_ALLOWED` | 预留给后续；当前 LLM 意图与 LLM 草稿一律不自动发送 |

安全边界：

- 模型只返回 `LLMIntentOutput` / `LLMDraftSuggestion` 两种结构，`extra="forbid"`。
- LLM 识别的意图不推进 case 状态，只生成待审草稿。
- LLM 草稿若包含 AD/CVD 结论、银行账户信息、其他 case 编号或非代理商域名邮箱，直接拒绝并回退模板。
- 检索强制 `case_id` 隔离；对话历史仍按 `thread_id` 直读，不走检索。


## 本地跑通

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

autoemail init-db               # 建表（默认 sqlite）
autoemail seed-demo             # 3 个代理商 / 1 个委托 / case BOND-2026-0142
autoemail ingest tests/fixtures # 回放示例邮件，输出归属结果
autoemail import ./history_eml     # 批量导入历史邮件：按时间排序，默认不自动发送
autoemail mail check              # 验证 IMAP/SMTP 登录，不收取也不发送
autoemail mail poll               # 拉取未读邮件并处理；默认不自动发送
autoemail eval export labels.jsonl # 导出评测集：predicted 已填，人工改 expected
autoemail eval run labels.jsonl    # 回归：输出意图/归属/归属方式准确率与不一致明细

# 按状态过滤审核队列：/drafts?status=pending；Case 列表：/cases?group=active
pytest                          # 规则与归属的单元测试

# 主动询价（dry_run：只生成审核草稿，不发送）
python -m autoemail.cli initiate --commission-id 1 --agent-id 2 --agent-email brittney@borderbuddy.com --auto-mode dry_run

# 催办任务（默认 3 天后到期；先生成审核草稿，不自动发送）
python -m autoemail.cli followup scan
python -m autoemail.cli followup run

# 草稿审核队列
python -m autoemail.cli draft list
python -m autoemail.cli draft show 1
python -m autoemail.cli draft edit 1 --reviewer ops@example.com --body "修改后的邮件正文"
python -m autoemail.cli draft reject 1 --reviewer ops@example.com --reason "信息不完整"
python -m autoemail.cli draft approve 1 --reviewer ops@example.com

# 影子模式统计：一致率、兜底分布、草稿建议结果
python -m autoemail.cli llm stats

# 切换 case 自动模式（off / dry_run / on）
python -m autoemail.cli case set-mode 1 --mode dry_run --actor ops@example.com

# Invoice 与付款回填（付款由人工执行，这里只记账）
python -m autoemail.cli invoice list --status unpaid
python -m autoemail.cli invoice pay 1 --method wire --reference WT-20261118 --reviewer ops@example.com

# 本地网页审核台（默认 http://127.0.0.1:8787）
# 可查看 case / 草稿 / 审计 / LLM 统计，并编辑、拒绝、批准草稿
# 批准前需勾选确认；SMTP 未配置时发送会被拦截
python -m autoemail.cli web
python -m autoemail.cli web --port 9000 --no-open
```

## 无人值守运行

邮箱密码/授权码配好后，用管理脚本把审核台和邮件守护跑成常驻服务，实现「传文件 + 选代理商之后，后续回复全自动」。

**管理脚本（agent.ps1）**

```powershell
# 启动：审核台(127.0.0.1:8787) + 邮件守护(30 秒轮询，自动回复 on 模式 case)
powershell -ExecutionPolicy Bypass -File agent.ps1 start

# 查看状态（进程 + 最近日志 logs/watch.log）
powershell -ExecutionPolicy Bypass -File agent.ps1 status

# 停止 / 重启
powershell -ExecutionPolicy Bypass -File agent.ps1 stop
powershell -ExecutionPolicy Bypass -File agent.ps1 restart
```

**邮件守护（等价的手动命令）**

```powershell
python -m autoemail.cli mail watch --interval 30 --allow-auto-send --log logs\watch.log
```

守护进程单次轮询失败（认证失败、网络抖动）不会退出，记日志后继续，等凭据恢复自动重连。日志在 `logs/watch.log`。

**开机自启（需管理员权限）**

以管理员身份打开 PowerShell，执行：

```powershell
$action = New-ScheduledTaskAction -Execute "C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail\run_agent.bat"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "autoemail-agent" -Action $action -Trigger $trigger -RunLevel Limited -Force
```

注册后，每次登录电脑自动运行 `run_agent.bat`（内部调用 `agent.ps1 start`），审核台 + 邮件守护常驻。

## 设计约束（改代码前先看）

1. **归属不可靠就绝不自动发送**。只有 L1–L4 允许 `auto_eligible`；`agent_single_active` 只能出草稿。
2. **所有出站必须过 `policy/gate.py`**。闸门是唯一出口，`notices` 只提示、不改变动作。
3. **对话历史按 `thread_id` 取，不走向量检索**。检索会跨 case 污染，是串户事故来源。
4. **金额比的是首年现金总支出**（含 CF5106 等必收附加费），不是裸报价。
5. **AD/CVD 声明必须有 `adcvd_attestation`**，模型不得自行断言"不适用"。
6. **有效证件可进模型（业务已批准），但出站含证件附件仍需人工确认**。

## 目录

```
src/autoemail/
  config.py        运行配置（AUTOEMAIL_ 前缀环境变量）
  db.py            连接与建表
  models.py        全部表结构
  textutil.py      归一化工具
  intent.py        确定性意图识别
  intent_llm.py    LLM 意图兜底
  llm.py           OpenAI 兼容 provider
  llm_schemas.py   模型结构化输出
  llm_context.py   受限上下文构建
  rag.py           case 隔离的内存检索
  drafts.py        草稿生成 / 审核
  quotes.py        报价入库与金额判定
  invoices.py      Invoice 去重落库与付款回填
  outbound.py      统一出站服务
  state.py         case 状态机
  followups.py     催办任务
  initiate.py      主动询价
  pipeline.py      入站处理主链路
  importing.py     历史邮件批量导入
  evaluation.py    评测集导出与回归
  web.py           本地网页审核台
  channels/        渠道适配（base / eml / imap）
  ingest/          解析（含发票 PDF 文本）/ 线程 / 归属
  policy/          金额规则 / 发送闸门
  notify/          企业微信
  audit.py         审计写入
  cli.py           命令行入口
tests/             单元测试与 eml 样例
```
