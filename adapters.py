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

    def send(self, payload: str, timeout: float) -> str:
        raise NotImplementedError


class ApiAdapter(Adapter):
    label = "api"

    def __init__(self, base_url: str, api_key: str, model: str):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key, self.model = api_key, model

    def send(self, payload: str, timeout: float) -> str:
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": payload}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=max(1, timeout)) as r:
                data = json.loads(r.read().decode())
            return data["choices"][0]["message"]["content"]
        except (socket.timeout, TimeoutError):
            raise TimeoutError(f"API 超时: {self.url}")
        except urllib.error.HTTPError as e:
            raise AdapterError(f"API 返回 {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), socket.timeout):
                raise TimeoutError(f"API 超时: {self.url}")
            raise AdapterError(f"API 连接失败: {e}")
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise AdapterError(f"API 响应格式不符: {e}")


class ClipboardAdapter(Adapter):
    label = "clipboard"

    def __init__(self, put_clipboard, notify_wait, launch_command: str = ""):
        """put_clipboard(text)：线程安全地把文本写入系统剪贴板（由 UI 注入）。
        notify_wait(payload)：进入等待用户回传时通知 UI（由 UI 注入）。
        launch_command：可选，等待粘贴前尝试启动/聚焦的目标程序路径。"""
        self._put = put_clipboard
        self._notify = notify_wait
        self.launch_command = launch_command
        self._evt = threading.Event()
        self._result = None

    def send(self, payload: str, timeout: float) -> str:
        self._evt.clear()
        self._result = None
        self._put(payload)
        if self.launch_command:
            try:
                os.startfile(self.launch_command)  # Windows：启动/聚焦目标程序
            except OSError:
                pass  # 聚焦失败不阻塞流程，剪贴板内容已就绪
        self._notify(payload)
        if not self._evt.wait(max(0.5, timeout)):
            raise TimeoutError("等待剪贴板回传超时")
        if self._result is None:
            raise Aborted("用户终止")
        return self._result

    def deliver(self, text: str):
        """UI 在用户粘贴回产出并点击回传时调用。"""
        if text and text.strip():
            self._result = text
            self._evt.set()

    def abort(self):
        self._result = None
        self._evt.set()
