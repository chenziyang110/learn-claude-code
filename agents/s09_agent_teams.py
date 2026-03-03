#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
s09_agent_teams.py - 智能体小队（Agent Teams）

持久化命名智能体，基于文件的 JSONL 收件箱。每个队员在独立线程中运行自己的 agent 循环，
通过仅追加的收件箱通信。

    子智能体 (s04): 创建 -> 执行 -> 返回摘要 -> 销毁
    队员 (s09):    创建 -> 工作 -> 空闲 -> 工作 -> ... -> 关闭

    .team/config.json                   .team/inbox/           .team/chat_log.jsonl
    +----------------------------+      +------------------+    +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |    | 完整对话记录      |
    |  "members": [              |      | bob.jsonl        |    | 仅追加不删除      |
    |    {"name":"alice",        |      | lead.jsonl       |    | (谁→谁、内容、时间)|
    |     "role":"coder",        |      +------------------+    +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "修 bug"):
    +----------------------------+        open("alice.jsonl", "a").write(msg)
                                          同时 append 到 chat_log.jsonl

                                        read_inbox("alice"):
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]
         |                                open("alice.jsonl", "w").close()
         v                                return msgs  # 清空
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | ... 执行工具      |      | ... 等待 ...     |
    | status -> idle   |      |                  |
    +------------------+      +------------------+

    5 种消息类型（均已声明，此处未全部处理）：
    +-------------------------+-----------------------------------+
    | message                 | 普通文本消息                       |
    | broadcast               | 发给所有队员                       |
    | shutdown_request        | 请求优雅关闭 (s10)                |
    | shutdown_response       | 同意/拒绝关闭 (s10)                |
    | plan_approval_response  | 同意/拒绝计划 (s10)               |
    +-------------------------+-----------------------------------+

要点：「能互相沟通的队员。」
"""

import json
import os
import subprocess
import sys
import threading
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
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

SYSTEM = f"你是工作目录 {WORKDIR} 下的小队负责人。请通过收件箱创建队员并与之通信。"

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


# -- MessageBus：每个队员一个 JSONL 收件箱；对话记录单独持久化到 chat_log.jsonl --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.chat_log_path = self.dir.parent / "chat_log.jsonl"

    def _append_chat_log(self, record: dict):
        """仅追加对话记录，不删除，类似聊天记录。"""
        self.chat_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.chat_log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"错误：无效类型 '{msg_type}'。有效类型：{VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        # 完整对话记录单独追加到 chat_log，永不删除
        log_record = {**msg, "to": to}
        self._append_chat_log(log_record)
        return f"已向 {to} 发送 {msg_type}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text(encoding="utf-8", errors="replace").strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("", encoding="utf-8")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已向 {count} 名队员广播"


BUS = MessageBus(INBOX_DIR)


# -- TeammateManager：持久化命名智能体，config.json 管理 --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8", errors="replace"))
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8", errors="replace")

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"错误：'{name}' 当前状态为 {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"已创建队员 '{name}'（角色：{role}）"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        sys_prompt = (
            f"你是 '{name}'，角色：{role}，工作目录 {WORKDIR}。"
            f"使用 send_message 与其他人通信。请完成你的任务。"
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        for _ in range(50):
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": sys_prompt}] + messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:
                break
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
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    output = self._exec(name, tc.function.name, args)
                    print(f"  [{name}] {tc.function.name}: {str(output)[:120]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(output),
                    })
            else:
                messages.append({"role": "assistant", "content": text})
                break
        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            member["status"] = "idle"
            self._save_config()

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2, ensure_ascii=False)
        return f"未知工具：{tool_name}"

    def _teammate_tools(self) -> list:
        return [
            {"type": "function", "function": {"name": "bash", "description": "执行一条 shell 命令。",
             "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
            {"type": "function", "function": {"name": "read_file", "description": "读取文件内容。",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "write_file", "description": "将内容写入文件。",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "edit_file", "description": "在文件中精确替换一段文本。",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
            {"type": "function", "function": {"name": "send_message", "description": "向某位队员发消息。",
             "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}}},
            {"type": "function", "function": {"name": "read_inbox", "description": "读取并清空自己的收件箱。",
             "parameters": {"type": "object", "properties": {}}}},
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "暂无队员。"
        lines = [f"小队：{self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']}（{m['role']}）：{m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现 --
def _safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区：{p}")
    return path


def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
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


def _run_read(path: str, limit: int = None) -> str:
    try:
        lines = _safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...（还有 {len(lines) - limit} 行）"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8", errors="replace")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text(encoding="utf-8", errors="replace")
        if old_text not in c:
            return f"错误：在 {path} 中未找到指定文本"
        fp.write_text(c.replace(old_text, new_text, 1), encoding="utf-8", errors="replace")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


# -- 负责人工具派发（9 个工具）--
TOOL_HANDLERS = {
    "bash":            lambda **kw: _run_bash(kw["command"]),
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":  lambda **kw: TEAM.list_all(),
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":      lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False),
    "broadcast":       lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}

# OpenAI 兼容格式：type=function, function={name, description, parameters}
TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "执行一条 shell 命令。（Windows 下为 PowerShell）",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取文件内容。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "将内容写入文件。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "在文件中精确替换一段文本。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "spawn_teammate", "description": "创建一名持久化队员，在独立线程中运行。",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}}},
    {"type": "function", "function": {"name": "list_teammates", "description": "列出所有队员的姓名、角色与状态。",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "send_message", "description": "向某位队员的收件箱发消息。",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}}},
    {"type": "function", "function": {"name": "read_inbox", "description": "读取并清空负责人的收件箱。",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "broadcast", "description": "向所有队员发一条消息。",
     "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
]


def agent_loop(messages: list):
    """OpenAI 兼容：chat.completions + tool_calls / role=tool；每次调用前注入收件箱消息。"""
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>\n{json.dumps(inbox, indent=2, ensure_ascii=False)}\n</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "已记录收件箱消息。",
            })
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
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                handler = TOOL_HANDLERS.get(tc.function.name)
                try:
                    output = handler(**args) if handler else f"未知工具：{tc.function.name}"
                except Exception as e:
                    output = f"错误：{e}"
                print(f"> {tc.function.name}: {str(output)[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(output),
                })
        else:
            messages.append({"role": "assistant", "content": text})
            if text:
                print(text)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", "退出", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
