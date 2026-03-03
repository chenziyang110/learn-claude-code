#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
s08_background_tasks.py - 后台任务（Background Tasks）

在后台线程中执行命令。每次调用 LLM 前会先排空通知队列，将完成结果注入对话。

    主线程                    后台线程
    +-----------------+        +-----------------+
    | agent 循环      |        | 任务执行        |
    | ...             |        | ...             |
    | [LLM 调用] <---+------- | enqueue(result) |
    |  ^排空队列      |        +-----------------+
    +-----------------+

    时间线：
    Agent ----[派发 A]----[派发 B]----[其他工作]----
                 |              |
                 v              v
              [A 运行]      [B 运行]        （并行）
                 |              |
                 +-- 通知队列 --> [结果注入]

要点：「发了就不等——命令在后台跑，代理不阻塞。」
"""

import json
import os
import subprocess
import sys
import threading
import uuid
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

SYSTEM = f"你是工作目录 {WORKDIR} 下的编程代理。耗时较长的命令请用 background_run 在后台执行。"


# -- BackgroundManager：后台执行 + 通知队列 --
class BackgroundManager:
    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []  # 已完成任务结果
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """启动后台线程，立即返回 task_id。"""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return f"后台任务 {task_id} 已启动：{command[:80]}"

    def _execute(self, task_id: str, command: str):
        """线程目标：执行子进程，捕获输出，推入队列。"""
        try:
            if IS_WINDOWS:
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
                    timeout=300,
                )
            else:
                r = subprocess.run(
                    command,
                    shell=True,
                    cwd=WORKDIR,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "错误：超时（300 秒）"
            status = "timeout"
        except Exception as e:
            output = f"错误：{e}"
            status = "error"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "（无输出）"
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "（无输出）")[:500],
            })

    def check(self, task_id: str = None) -> str:
        """查询单个任务状态或列出全部。"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误：未知任务 {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '（运行中）'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "暂无后台任务。"

    def drain_notifications(self) -> list:
        """返回并清空所有待处理的完成通知。"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


BG = BackgroundManager()


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
    "background_run": lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

# OpenAI 兼容格式：type=function, function={name, description, parameters}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条 shell 命令。（阻塞式；Windows 下为 PowerShell）",
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
            "name": "background_run",
            "description": "在后台线程中执行命令，立即返回 task_id。",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_background",
            "description": "查询后台任务状态；不传 task_id 则列出全部。",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}},
        },
    },
]


def agent_loop(messages: list):
    """OpenAI 兼容：chat.completions + tool_calls / role=tool 消息格式；每次 LLM 调用前注入后台完成通知。"""
    while True:
        # 排空后台通知，在 LLM 调用前注入为系统/用户消息
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            messages.append({"role": "assistant", "content": "已记录后台结果。"})
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
            for tc in tool_calls:
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
        else:
            messages.append({"role": "assistant", "content": text})
            if text:
                print(text)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "退出", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
