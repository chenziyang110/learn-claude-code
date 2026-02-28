#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
s06_context_compact.py - 上下文压缩（Compact）

三层压缩流水线，让代理可以持续运行：

    每轮：
    +------------------+
    | 工具调用结果       |
    +------------------+
            |
            v
    [第一层：micro_compact]     （静默，每轮执行）
      将最近 3 条以外的 tool 结果替换为「[此前：已使用 {tool_name}]」
            |
            v
    [检查：tokens > 50000?]
       |               |
       否               是
       |               |
       v               v
    继续    [第二层：auto_compact]
              将完整对话保存到 .transcripts/
              调用 LLM 总结对话
              用 [总结] 替换全部消息
                    |
                    v
            [第三层：compact 工具]
              模型主动调用 compact -> 立即总结
              与 auto 相同逻辑，手动触发

要点：「代理可以策略性遗忘，从而持续工作。」
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

# 支持 OpenAI / DeepSeek / 本地代理等 OpenAI 兼容接口
api_key = os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY") or os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
client = OpenAI(api_key=api_key, base_url=base_url) if api_key else OpenAI()
MODEL = os.getenv("MODEL_ID", "deepseek-chat")

WORKDIR = Path.cwd()
SYSTEM = f"你是工作目录 {WORKDIR} 下的编程代理。使用工具完成任务。"

THRESHOLD = 5000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
KEEP_RECENT = 3


def estimate_tokens(messages: list) -> int:
    """粗略 token 估算：约 4 字符/token。"""
    return len(str(messages)) // 4


# -- 第一层：micro_compact - 将较早的 tool 结果替换为占位符 --
def micro_compact(messages: list) -> list:
    # 收集所有 role="tool" 的消息 (idx, msg)
    tool_result_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tool_result_indices.append((i, msg))
    if len(tool_result_indices) <= KEEP_RECENT:
        return messages
    # 从之前的 assistant 消息中建立 tool_call_id -> function name 的映射
    tool_name_map = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                if tid and fn:
                    name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "unknown")
                    tool_name_map[tid] = name
    # 只保留最近 KEEP_RECENT 条完整结果，其余替换为占位
    to_clear = tool_result_indices[:-KEEP_RECENT]
    for idx, msg in to_clear:
        content = msg.get("content") or ""
        if isinstance(content, str) and len(content) > 100:
            tid = msg.get("tool_call_id", "")
            tool_name = tool_name_map.get(tid, "unknown")
            messages[idx] = {**msg, "content": f"[此前：已使用 {tool_name}]"}
    return messages


# -- 第二层：auto_compact - 保存笔录、总结、替换消息 --
def auto_compact(messages: list) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w", encoding="utf-8", errors="replace") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print(f"[笔录已保存: {transcript_path}]")
    conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    # 用 LLM 总结
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": "请对以下对话做连续性总结，需包含："
                "1）已完成的事项；2）当前状态；3）关键决策。"
                "简洁但保留关键信息。\n\n" + conversation_text,
            }
        ],
        max_tokens=2000,
    )
    summary = (response.choices[0].message.content or "").strip()
    return [
        {
            "role": "user",
            "content": f"[对话已压缩。笔录：{transcript_path}]\n\n{summary}",
        },
        {
            "role": "assistant",
            "content": "已了解。我已从总结中获取上下文，继续执行。",
        },
    ]


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：已拦截危险命令"
    try:
        if IS_WINDOWS:
            # 强制 PowerShell 以 UTF-8 输出，避免中文系统代码页（GBK）导致乱码
            utf8_prefix = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", utf8_prefix + command],
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        else:
            r = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=120,
            )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "（无输出）"
    except subprocess.TimeoutExpired:
        return "错误：超时（120 秒）"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...（还有 {len(lines) - limit} 行）"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8", errors="replace")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return f"错误：在 {path} 中未找到指定文本"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8", errors="replace")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact": lambda **kw: "已请求手动压缩。",
}

# OpenAI 兼容格式：type=function, function={name, description, parameters}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条 shell 命令。（Windows 下为 PowerShell）",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "在文件中精确替换一段文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compact",
            "description": "触发手动对话压缩（总结当前对话并替换为摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "description": "总结中希望保留的重点"},
                },
            },
        },
    },
]


def agent_loop(messages: list):
    """OpenAI 兼容：chat.completions + tool_calls / role=tool 消息格式。"""
    while True:
        micro_compact(messages)
        if estimate_tokens(messages) > THRESHOLD:
            print("[自动压缩已触发]")
            messages[:] = auto_compact(messages)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        msg = response.choices[0].message
        text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            )
            manual_compact = False
            for tc in tool_calls:
                if tc.function.name == "compact":
                    manual_compact = True
                    output = "正在压缩..."
                else:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    handler = TOOL_HANDLERS.get(tc.function.name)
                    try:
                        output = handler(**args) if handler else f"未知工具: {tc.function.name}"
                    except Exception as e:
                        output = f"错误：{e}"
                print(f"> {tc.function.name}: {str(output)[:200]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (str(output))[:50000],
                    }
                )
            if manual_compact:
                print("[手动压缩]")
                messages[:] = auto_compact(messages)
        else:
            messages.append({"role": "assistant", "content": text})
            print(text)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "退出", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
