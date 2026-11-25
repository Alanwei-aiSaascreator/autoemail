"""运行配置。所有项可用 AUTOEMAIL_ 前缀环境变量覆盖。"""

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="AUTOEMAIL_", extra="ignore"
    )

    db_url: str = "sqlite:///./autoemail.db"

    # 业务硬规则（V0.2 §7.2）
    approval_line_usd: Decimal = Decimal("1000")
    # 异常价提示阈值：仅提示，不改变动作
    anomaly_ratio: Decimal = Decimal("1.3")

    # 邮件渠道：平台未定，先支持 IMAP/SMTP 与本地 eml 回放
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_addr: str = ""

    # 发送节流：每封邮件之间的最小间隔（秒），避免触发邮件服务器 fail2ban（连接频率/连续认证失败）
    send_interval_seconds: float = 3.0

    # 企业微信群机器人
    wecom_webhook: str = ""
    # 飞书群机器人（自定义机器人 webhook）
    feishu_webhook: str = ""
    # 飞书自建应用凭据：用于读取委托资料文档（粘贴飞书链接导入委托）
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # 每日随机询价：文件齐全的委托每天随机选 N 家代理商发询价，收到报价即推飞书
    daily_inquiry_enabled: bool = True
    daily_inquiry_batch_size: int = 20

    # 自动填表产物目录：收到代理商表格自动填充后的 PDF 落盘位置
    filled_forms_dir: str = "./filled_forms"

    # 审核台监听地址：默认仅本机可访问；运行设备可设 0.0.0.0 开放给局域网
    web_host: str = "127.0.0.1"

    # 自动发送时间窗：自动邮件从纽约时间每天 09:00 起才允许发出（人工批准不受限）
    auto_send_timezone: str = "America/New_York"
    auto_send_start_et: str = "09:00"

    # LLM / RAG 默认关闭，启用后仍只能生成受控结果
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_vision_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 20.0
    llm_max_output_tokens: int = 1200
    llm_max_input_chars: int = 30000
    llm_intent_fallback_enabled: bool = False
    llm_draft_enabled: bool = False
    llm_auto_send_allowed: bool = False
    llm_shadow_mode: bool = False
    rag_enabled: bool = False
    rag_top_k: int = 5
    rag_max_chars: int = 8000


settings = Settings()
