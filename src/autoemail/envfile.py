"""读写本地 .env 的小工具：保留注释与顺序，只替换目标键。"""
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _format(value: str) -> str:
    value = value.replace("\r", "").replace("\n", "")
    if value == "":
        return ""
    if re.search(r"[\s#\"']", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env(updates: dict[str, str], path: str | Path | None = None) -> Path:
    """把 updates 写入 .env；已存在的键替换，缺失的键追加。"""
    target = Path(path) if path else ENV_PATH
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={_format(updates[key])}")
                seen.add(key)
                continue
        output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={_format(value)}")

    target.write_text("\n".join(output) + "\n", encoding="utf-8")
    return target
