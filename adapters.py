"""适配器框架。统一接口：send(payload, timeout) -> str。

两类适配器：
- ApiAdapter：调用任何 OpenAI 兼容 HTTP 端点（用户自填 base_url / api_key / model，App 不预设）。
- ClipboardAdapter：半自动——把内容写入剪贴板，用户手动粘贴到目标 Agent，
  再把对方回复粘贴回 App（由 UI 调用 deliver() 回传）。
"""
import json
import os
import socket
import threading
import urllib.error
import urllib.request


class Aborted(RuntimeError):
    """用户终止会话。"""


class AdapterError(RuntimeError):
    """适配器调用失败（非超时）。"""


class Adapter:
    label = "adapter"

    def supports(self, role: str) -> bool:
        """该适配器能否担任此角色。两类适配器均可担任 A 或 B；可用性探测留待 P1（F18）。"""
        return role in ("A", "B")

    def send(self, payload: str, timeout: float) -> str:
        raise NotImplementedError


class ApiAdapter(Adapter):
    label = "api"

    def __init__(self, base_url: str, api_key: str, model: str):
        base = base_url.rstrip("/")
        # 用户粘贴完整端点时不再重复拼接（F14）
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self.api_key, self.model = api_key, model

    def send(self, payload: str, timeout: float) -> str:
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": payload}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=max(0.05, timeout)) as r:
                data = json.loads(r.read().decode())
            if not isinstance(data, dict):
                raise AdapterError("API 响应顶层不是 JSON 对象")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise AdapterError("API 响应缺少非空 choices")
            first = choices[0]
            if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
                raise AdapterError("API 响应缺少 message 对象")
            content = first["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise AdapterError("API 响应 content 为空或非字符串")
            return content
        except (socket.timeout, TimeoutError):
            raise TimeoutError(f"API 超时: {self.url}")
        except urllib.error.HTTPError as e:
            raise AdapterError(f"API 返回 {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), socket.timeout):
                raise TimeoutError(f"API 超时: {self.url}")
            raise AdapterError(f"API 连接失败: {e}")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise AdapterError(f"API 响应格式不符: {e}")


class ClipboardAdapter(Adapter):
    label = "clipboard"

    def __init__(self, put_clipboard, notify_wait, launch_command: str = ""):
        """put_clipboard(text)：线程安全地把文本写入系统剪贴板（由 UI 注入）。
        notify_wait(adapter, payload, token)：进入等待时通知 UI，token 标识本次等待，
        UI 回传时必须带回同一 token，防止上一轮的迟到回传串入下一轮（F6）。
        launch_command：可选，等待粘贴前尝试启动/聚焦的目标程序路径。"""
        self._put = put_clipboard
        self._notify = notify_wait
        self.launch_command = launch_command
        self._evt = threading.Event()
        self._abort = threading.Event()
        self._token = 0
        self._active = False
        self._result = None

    def is_waiting(self) -> bool:
        """是否正处于一次未完成的 send 等待（UI 据此路由回传，F1）。"""
        return self._active

    def send(self, payload: str, timeout: float) -> str:
        if self._abort.is_set():  # 独立事件，不被下面的 clear() 吞掉（F8）
            raise Aborted("剪贴板适配器已被终止")
        self._token += 1
        self._active = True
        self._evt.clear()
        self._result = None
        try:
            self._put(payload)
            if self.launch_command:
                try:
                    os.startfile(self.launch_command)  # Windows：启动/聚焦目标程序
                except OSError:
                    pass  # 聚焦失败不阻塞流程，剪贴板内容已就绪
            self._notify(self, payload, self._token)
            if not self._evt.wait(max(0.05, timeout)):
                raise TimeoutError("等待剪贴板回传超时")
            if self._result is None:
                raise Aborted("用户终止")
            return self._result
        finally:
            self._active = False

    def deliver(self, token: int, text: str) -> bool:
        """UI 在用户粘贴回产出时调用。token 不匹配当前等待（迟到/失效）则丢弃。"""
        if not self._active or token != self._token or not (text and text.strip()):
            return False
        self._result = text
        self._evt.set()
        return True

    def abort(self):
        self._abort.set()
        self._evt.set()
