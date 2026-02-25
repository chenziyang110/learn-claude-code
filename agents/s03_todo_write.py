#!/usr/bin/env python3
"""
s03_todo_write.py - TodoWrite（待办写入）

模型通过 TodoManager 跟踪自己的进度。若多轮未更新待办，
会注入提醒，督促其持续更新。

    +----------+      +-------+      +---------+
    |   用户   | ---> |  LLM  | ---> | 工具    |
    |  输入   |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager 状态      |
                    | [ ] 任务 A            |
                    | [>] 任务 B <- 进行中  |
                    | [x] 任务 C            |
                    +-----------------------+
                                |
                    若 rounds_since_todo >= 3:
                      注入 <reminder>

要点：“Agent 可以跟踪自己的进度，并且我能看到。”
"""

import json
import os
import subprocess
import sys
from pathlib import Path

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

# 工作目录为当前脚本所在目录
WORKDIR = Path(__file__).resolve().parent

SYSTEM = f"""你是工作目录 {WORKDIR} 下的编程助手。
使用 todo 工具规划多步任务：开始前标为 in_progress，完成后标为 completed。
优先用工具执行，少说多做。"""


# -- TodoManager：LLM 写入的结构化状态 --
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("最多允许 20 个待办")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"项 {item_id}：必须填写内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项 {item_id}：无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同一时间只能有一个任务为进行中")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "暂无待办。"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n（已完成 {done}/{len(self.items)} 项）")
        return "\n".join(lines)


TODO = TodoManager()


# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区：{p}")
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
            lines = lines[:limit] + [f"…（还有 {len(lines) - limit} 行）"]
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
            return f"错误：在 {path} 中未找到该文本"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8", errors="replace")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}

# OpenAI 兼容的 tools 格式：type=function, function={name, description, parameters}
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
            "name": "todo",
            "description": "更新任务列表，跟踪多步任务的进度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            },
                            "required": ["id", "text", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
]


# -- Agent 主循环与待办提醒注入（OpenAI 兼容）--
def agent_loop(messages: list):
    """OpenAI 兼容：chat.completions + tool_calls / tool 消息格式，并保留待办提醒注入。"""
    rounds_since_todo = 0
    while True:
        # 若连续 3 轮未更新待办，在最后一条用户消息前注入提醒
        if rounds_since_todo >= 3 and messages and messages[-1].get("role") == "user":
            content = messages[-1].get("content") or ""
            if isinstance(content, str) and not content.strip().startswith("<reminder>"):
                messages[-1]["content"] = "<reminder>请更新你的待办列表。</reminder>\n" + content

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
            messages.append({
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
            used_todo = False
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                name = tc.function.name
                handler = TOOL_HANDLERS.get(name)
                try:
                    output = handler(**args) if handler else f"未知工具：{name}"
                except Exception as e:
                    output = f"错误：{e}"
                print(f"> {name}: {str(output)[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (str(output) or "")[:50000],
                })
                if name == "todo":
                    used_todo = True
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        else:
            messages.append({"role": "assistant", "content": text})
            print(text)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "退出", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
