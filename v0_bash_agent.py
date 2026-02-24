#!/usr/bin/env python
"""
v0_bash_agent.py - 迷你 Claude Code：Bash 就是一切（核心约 50 行）

核心理念："Bash 就是一切"
======================================
这是编程智能体的终极简化版本。在构建 v1-v3 之后，
我们要问：智能体的本质是什么？

答案：一个工具（bash）+ 一个循环 = 完整的智能体能力。

为什么 Bash 就够了：
------------------
Unix 哲学说万物皆文件，万物皆可管道。
Bash 就是通往这个世界的大门：

    | 你需要        | Bash 命令                              |
    |---------------|----------------------------------------|
    | 读取文件      | cat, head, tail, grep                  |
    | 写入文件      | echo '...' > file, cat << 'EOF' > file |
    | 搜索          | find, grep, rg, ls                     |
    | 执行程序      | python, npm, make, 任意命令            |
    | **子智能体**  | python v0_bash_agent.py "任务"         |

最后一行是关键洞见：通过 bash 调用自身来实现子智能体！
无需 Task 工具，无需智能体注册表——仅靠进程派生实现递归。

子智能体工作原理：
------------------
    主智能体
      |-- bash: python v0_bash_agent.py "分析架构"
           |-- 子智能体（独立进程，全新历史记录）
                |-- bash: find . -name "*.py"
                |-- bash: cat src/main.py
                |-- 通过 stdout 返回摘要

进程隔离 = 上下文隔离：
- 子进程拥有自己的 history=[]
- 父进程将 stdout 作为工具结果捕获
- 递归调用支持无限嵌套

用法：
    # 交互模式
    python v0_bash_agent.py

    # 子智能体模式（由父智能体调用或直接运行）
    python v0_bash_agent.py "探索 src/ 并总结"
"""

from openai import OpenAI
from dotenv import load_dotenv
import subprocess
import sys
import os
import json

IS_WINDOWS = sys.platform == "win32"

load_dotenv(override=True)

# OpenAI 兼容接口：支持 DeepSeek、OpenAI、本地代理等
# 环境变量：OPENAI_API_KEY / API_KEY，OPENAI_BASE_URL / BASE_URL / ANTHROPIC_BASE_URL
api_key = os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY") or os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
client = OpenAI(api_key=api_key, base_url=base_url) if api_key else OpenAI()
MODEL = os.getenv("MODEL_ID", "deepseek-chat")

# 唯一的工具，能做所有事情（OpenAI 函数调用格式）
if IS_WINDOWS:
    _tool_desc = """执行 PowerShell 命令。常用模式：
- 读取：Get-Content/type, Select-String/findstr, Get-ChildItem/dir, gc, cat
- 写入：Set-Content, Out-File, Add-Content, ($content -replace 'old','new') | Set-Content file
- 子智能体：python v0_bash_agent.py '任务描述'（派生独立智能体，返回摘要）"""
else:
    _tool_desc = """执行 shell 命令。常用模式：
- 读取：cat/head/tail, grep/find/rg/ls, wc -l
- 写入：echo 'content' > file, sed -i 's/old/new/g' file
- 子智能体：python v0_bash_agent.py '任务描述'（派生独立智能体，返回摘要）"""

TOOL = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": _tool_desc,
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    }
}]

# 系统提示词：教模型如何有效使用 shell
# 注意子智能体指导——这是实现层级任务分解的方式
if IS_WINDOWS:
    SYSTEM = f"""你是一个 CLI 智能体，当前目录为 {os.getcwd()}。当前系统为 Windows，请使用 PowerShell 命令解决问题。

规则：
- 优先使用工具而非纯文字描述。先行动，再简要说明。
- 读取文件：Get-Content (gc/cat), Select-String, Get-ChildItem (ls/dir), Measure-Object
- 写入文件：Set-Content, Out-File, Add-Content，或 @' ... '@ | Set-Content file
- 路径分隔符：Windows 使用反斜杠 \\ 或正斜杠 /（PowerShell 两者均支持）
- 子智能体：对于复杂子任务，派生子智能体以保持上下文整洁：
  python v0_bash_agent.py "探索 src/ 并总结架构"

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
  python v0_bash_agent.py "探索 src/ 并总结架构"

何时使用子智能体：
- 任务需要读取大量文件（隔离探索过程）
- 任务独立且自包含
- 不想用中间细节污染当前对话

子智能体在隔离环境中运行，仅返回最终摘要。"""


def chat(prompt, history=None):
    """
    完整的智能体循环，封装在单个函数中。
    使用 OpenAI 兼容 API（chat/completions + 函数调用）。

    参数：
        prompt: 用户请求
        history: 对话历史（可变列表，元素为 {role, content} 或含 tool_calls）

    返回：
        模型的最终文本响应
    """
    if history is None:
        history = []

    history.append({"role": "user", "content": prompt})

    while True:
        # 1. 调用模型（OpenAI 兼容接口）
        kwargs = {
            "model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM}] + history,
            "tools": TOOL,
            "max_tokens": 8000,
        }
        response = client.chat.completions.create(**kwargs)

        msg = response.choices[0].message
        text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []

        # 2. 追加助手消息（可选含 tool_calls）
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

        # 3. 执行每个工具调用并将结果追加到历史记录
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
        # 子智能体模式：执行任务并打印结果
        # 父智能体通过 bash 派生子智能体的方式
        print(chat(sys.argv[1]))
    else:
        # 交互式 REPL 模式
        history = []
        while True:
            try:
                query = input("\033[36m>> \033[0m")  # 青色提示符
            except (EOFError, KeyboardInterrupt):
                break
            if query in ("q", "exit", ""):
                break
            print(chat(query, history))
