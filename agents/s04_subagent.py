#!/usr/bin/env python3
"""
s04_subagent.py - 子代理（Subagents）
提问：分四个智能体分别读取s1到s04代码的内容，然后简短总结这四个代码的用处和差别
用空消息列表 messages=[] 启动子代理。子代理在独立上下文中工作，
共享同一文件系统，完成后仅向父代理返回摘要。

    父代理                          子代理
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- 全新
    |                  |  派发任务   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   调用工具        |
    |   description="" |            |   追加结果        |
    |                  |  摘要返回  |                  |
    |   result = "..." | <--------- | 返回最后一段文本 |
    +------------------+             +------------------+
              |
    父代理上下文保持干净。
    子代理上下文被丢弃。

要点：「进程隔离带来免费的上下文隔离。」
"""

import json
import os
import subprocess
import sys
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
SYSTEM = f"你是工作目录 {WORKDIR} 下的编程代理。使用 task 工具将探索或子任务委派给子代理。"
SUBAGENT_SYSTEM = f"你是工作目录 {WORKDIR} 下的编程子代理。完成给定任务后，用简短摘要汇报结果。"


# -- 父/子代理共用的工具实现 --
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
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
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
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 子代理仅有基础工具，不含 task（避免递归派发）。OpenAI 兼容格式
CHILD_TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "执行一条 shell 命令。（Windows 下为 PowerShell）",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取文件内容。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "将内容写入文件。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "在文件中精确替换一段文本。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
]


# -- 子代理：全新上下文、限定工具、仅返回摘要（OpenAI 兼容）--
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]  # 全新上下文
    last_text = ""
    for _ in range(30):  # 安全上限
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUBAGENT_SYSTEM}] + sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        msg = response.choices[0].message
        last_text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []
        sub_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        if not tool_calls:
            break
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            handler = TOOL_HANDLERS.get(tc.function.name)
            output = handler(**args) if handler else f"未知工具: {tc.function.name}"
            sub_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": (str(output))[:50000],
            })
    return last_text or "（无摘要）"


# -- 父代理工具：基础工具 + task 派发器（OpenAI 兼容格式）--
PARENT_TOOLS = CHILD_TOOLS + [
    {"type": "function", "function": {"name": "task", "description": "用全新上下文启动子代理。子代理共享文件系统，但不共享对话历史。",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "任务简短描述"}}, "required": ["prompt"]}}},
]


def agent_loop(messages: list):
    """OpenAI 兼容：chat.completions + tool_calls / role=tool 消息格式。"""
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )
        msg = response.choices[0].message
        text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                name = tc.function.name
                if name == "task":
                    desc = args.get("description", "子任务")
                    print(f"> task（{desc}）: {(args.get('prompt') or '')[:80]}")
                    output = run_subagent(args.get("prompt", ""))
                else:
                    handler = TOOL_HANDLERS.get(name)
                    output = handler(**args) if handler else f"未知工具: {name}"
                print(f"  {str(output)[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (str(output))[:50000],
                })
        else:
            messages.append({"role": "assistant", "content": text})
            print(text)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "退出", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
