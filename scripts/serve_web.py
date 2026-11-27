"""常驻入口：启动网页服务（由计划任务/手工启动，不依赖任何会话）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from autoemail.web import serve

serve(open_browser=False)
