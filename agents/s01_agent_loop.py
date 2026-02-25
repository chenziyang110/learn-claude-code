#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

核心理念："Bash 就是一切"
======================================
一个工具（bash）+ 一个循环 = 完整的智能体能力。

为什么 Bash 就够了：
------------------
    | 你需要        | 命令                                   |
    |---------------|----------------------------------------|
    | 读取文件      | cat, head, tail, grep / Get-Content    |
    | 写入文件      | echo '...' > file / Set-Content        |
    | 搜索          | find, grep, rg, ls / Get-ChildItem     |
    | 执行程序      | python, npm, make, 任意命令            |
    | 子智能体      | python s01_agent_loop.py "任务"        |

子智能体工作原理：
------------------
    主智能体
      |-- bash: python s01_agent_loop.py "分析架构"
           |-- 子智能体（独立进程，全新历史记录）
                |-- bash: find . -name "*.py"
                |-- 通过 stdout 返回摘要

用法：
    # 交互模式
    python s01_agent_loop.py

    # 子智能体模式（由父智能体调用或直接运行）
    python s01_agent_loop.py "探索 src/ 并总结"
"""

import json
import os
import subprocess
import sys

from dotenv import load_dotenv
from openai import OpenAI

IS_WINDOWS = sys.platform == "win32"

# 在 Windows 下将 stdout/stderr 强制设为 UTF-8，避免 GBK 无法编码替换字符
if IS_WINDOWS:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

# 支持 OpenAI / DeepSeek / 本地代理等 OpenAI 兼容接口
api_key = os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY") or os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
client = OpenAI(api_key=api_key, base_url=base_url) if api_key else OpenAI()
MODEL = os.getenv("MODEL_ID", "deepseek-chat")

if IS_WINDOWS:
    _tool_desc = """执行 PowerShell 命令。常用模式：
- 读取：Get-Content/type, Select-String/findstr, Get-ChildItem/dir, gc, cat
- 写入：Set-Content, Out-File, Add-Content, ($content -replace 'old','new') | Set-Content file
- 子智能体：python s01_agent_loop.py '任务描述'（派生独立智能体，返回摘要）"""
else:
    _tool_desc = """执行 shell 命令。常用模式：
- 读取：cat/head/tail, grep/find/rg/ls, wc -l
- 写入：echo 'content' > file, sed -i 's/old/new/g' file
- 子智能体：python s01_agent_loop.py '任务描述'（派生独立智能体，返回摘要）"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": _tool_desc,
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]

if IS_WINDOWS:
    SYSTEM = f"""你是一个 CLI 智能体，当前目录为 {os.getcwd()}。当前系统为 Windows，请使用 PowerShell 命令解决问题。

规则：
- 优先使用工具而非纯文字描述。先行动，再简要说明。
- 读取文件：Get-Content (gc/cat), Select-String, Get-ChildItem (ls/dir), Measure-Object
- 写入文件：Set-Content, Out-File, Add-Content，或 @' ... '@ | Set-Content file
- 路径分隔符：Windows 使用反斜杠 \\ 或正斜杠 /（PowerShell 两者均支持）
- 子智能体：对于复杂子任务，派生子智能体以保持上下文整洁：
  python s01_agent_loop.py "探索 src/ 并总结架构"

何时使用子智能体：
- 任务需要读取大量文件（隔离探索过程）
- 任务独立且自包含
- 不想用中间细节污染当前对话

子智能体在隔离环境中运行，仅返回最终摘要。"""
else:
    SYSTEM = f"""你是一个 CLI 智能体，当前目录为 {os.getcwd()}。请使用 bash 命令解决问题。

规则：
- 优先使用工具而非纯文字描述。先行动，再简要说明。
- 读取文件：cat, grep, find, rg, ls, head, tail
- 写入文件：echo '...' > file, sed -i, 或 cat << 'EOF' > file
- 子智能体：对于复杂子任务，派生子智能体以保持上下文整洁：
  python s01_agent_loop.py "探索 src/ 并总结架构"

何时使用子智能体：
- 任务需要读取大量文件（隔离探索过程）
- 任务独立且自包含
- 不想用中间细节污染当前对话

子智能体在隔离环境中运行，仅返回最终摘要。"""


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(prompt: str, history: list = None) -> str:
    """
    完整的智能体循环，封装在单个函数中。

    参数：
        prompt:  用户请求
        history: 对话历史（可变列表），传入同一列表可保持多轮上下文

    返回：
        模型的最终文本响应
    """
    if history is None:
        history = []

    history.append({"role": "user", "content": prompt})

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + history,
            tools=TOOLS,
            max_tokens=8000,
        )

        msg = response.choices[0].message
        text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            history.append({
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
            })
        else:
            history.append({"role": "assistant", "content": text})
            return text

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            cmd = args.get("command", "")
            print(f"\033[33m$ {cmd}\033[0m")

            try:
                if IS_WINDOWS:
                    run_args = dict(
                        args=["powershell", "-NoProfile", "-Command", cmd],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=300,
                        cwd=os.getcwd(),
                    )
                else:
                    run_args = dict(
                        args=cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        cwd=os.getcwd(),
                    )
                out = subprocess.run(**run_args)
                output = out.stdout + out.stderr
            except subprocess.TimeoutExpired:
                output = "（超时，已等待 300 秒）"

            print(output or "（无输出）")
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": (output or "")[:50000],
            })


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # 子智能体模式：执行任务并打印结果，供父智能体捕获
        print(agent_loop(sys.argv[1]))
    else:
        # 交互式 REPL 模式
        history = []
        while True:
            try:
                query = input("\033[36ms01 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            print(agent_loop(query, history))
            print()
