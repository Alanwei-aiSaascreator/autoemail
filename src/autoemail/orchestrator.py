"""function calling 编排器：LLM 自动决定调用哪些工具，多轮执行直到产出最终答案。"""

import json
from typing import Any

from .llm import HttpxOpenAICompatibleProvider
from .toolkit import TOOL_REGISTRY, tool_schemas


def _serialize(result: Any) -> str:
    """把工具结果转成 JSON 字符串，供回传给 LLM。"""
    if result is None:
        return "null"
    if hasattr(result, "model_dump"):  # pydantic
        return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, default=str)
    return str(result)


def run_tool_agent(
    user_message: str,
    *,
    system_prompt: str = "你是档案处理助手，可以根据需要调用工具完成任务。",
    tools: list[dict] | None = None,
    max_rounds: int = 8,
    model: str | None = None,
) -> dict:
    """多轮 tool calling 编排。

    返回 {"final_answer": str|None, "tool_calls": [...], "error": str|None}
    """
    provider = HttpxOpenAICompatibleProvider()
    tool_list = tools or tool_schemas()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    calls_log: list[dict] = []

    for _ in range(max_rounds):
        try:
            data = provider.complete_with_tools(messages, tool_list, model=model)
        except Exception as exc:  # noqa: BLE001
            return {"final_answer": None, "tool_calls": calls_log, "error": str(exc)}

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = msg.get("tool_calls")

        assistant_msg: dict = {"role": "assistant", "content": msg.get("content")}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            return {"final_answer": msg.get("content"), "tool_calls": calls_log, "error": None}

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool = TOOL_REGISTRY.get(fn_name)
            if tool is None:
                result_str = json.dumps({"error": f"未知工具 {fn_name}"}, ensure_ascii=False)
            else:
                try:
                    result = tool.func(**args)
                except Exception as exc:  # noqa: BLE001
                    result_str = json.dumps(
                        {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                    )
                else:
                    result_str = _serialize(result)
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
            )
            calls_log.append({"tool": fn_name, "arguments": args, "result": result_str[:200]})

    return {"final_answer": None, "tool_calls": calls_log, "error": "达到最大轮数仍未产出最终答案"}
