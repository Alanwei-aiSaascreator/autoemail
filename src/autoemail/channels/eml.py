"""本地 .eml 回放通道：无需邮箱即可跑通链路，也是历史邮件评测集导入的入口。"""

from pathlib import Path

from ..ingest.parse import parse_email_bytes
from .base import NormalizedMessage, OutboundMessage, SendResult


class EmlDirectoryChannel:
    name = "eml"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _files(self) -> list[Path]:
        if self.root.is_file():
            return [self.root]
        return sorted(p for p in self.root.rglob("*") if p.suffix.lower() == ".eml")

    def fetch_since(self, cursor: str | None = None) -> list[NormalizedMessage]:
        # cursor 为已处理文件名，按字典序推进，便于回放时断点续跑
        files = self._files()
        if cursor:
            files = [p for p in files if p.name > cursor]
        return [parse_email_bytes(p.read_bytes(), channel=self.name) for p in files]

    def send(self, outbound: OutboundMessage) -> SendResult:
        return SendResult(ok=False, error="eml 通道只用于回放，不支持发送")
