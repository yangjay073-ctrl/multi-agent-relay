"""适配器框架。统一接口：send(payload, timeout) -> str。

两类适配器：
- ApiAdapter：调用任何 OpenAI 兼容 HTTP 端点（用户自填 base_url / api_key / model，App 不预设）。
- ClipboardAdapter：半自动——把内容写入剪贴板，用户手动粘贴到目标 Agent，
  再把对方回复粘贴回 App（由 UI 调用 deliver() 回传）。
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request


class Aborted(RuntimeError):
    """用户终止会话。"""


def focus_window_by_exe(exe_name: str) -> bool:
    """把属于 exe_name（如 notepad.exe）的可见窗口置前（F17，仅 Windows）。
    成功 True；找不到或非 Windows 返回 False。"""
    import ctypes
    import ctypes.wintypes as w
    if sys.platform != "win32":
        return False
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def on_window(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(512)
        size = w.DWORD(512)
        try:
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) \
                    and os.path.basename(buf.value).lower() == exe_name.lower():
                hits.append(hwnd)
        finally:
            kernel32.CloseHandle(h)
        return True

    user32.EnumWindows(on_window, 0)
    if not hits:
        return False
    user32.ShowWindow(hits[0], 9)  # SW_RESTORE
    user32.SetForegroundWindow(hits[0])  # 前台锁定限制下可能失败，窗口至少已还原
    return True


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
        return self._request(payload, timeout)

    def test_connection(self) -> str:
        """设置页「测试连接」（F18）：走一次完整请求链路验证端点/key/model。"""
        return self._request("连接测试，请只回复：ok", 30.0)

    def _request(self, payload: str, timeout: float) -> str:
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": payload}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            try:
                with urllib.request.urlopen(req, timeout=max(0.05, timeout)) as r:
                    raw = r.read().decode()
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise AdapterError(f"API 响应非 JSON: {e}; 响应前200字符: {raw[:200]!r}")
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


class CliAdapter(Adapter):
    """本地命令行 Agent：任务包经 stdin 传入，stdout 即产出（如 codex exec / claude -p）。

    command 为完整命令行。Windows 下按 CreateProcess 规则解析（引号由用户按
    Windows 习惯书写）；POSIX 下经 shlex 切分不经 shell。cwd 为可选工作目录——
    执行型 Agent 会在该目录里真实干活。注意：配置此适配器即授权该命令本地执行。
    """
    label = "cli"

    def __init__(self, command: str, cwd: str = ""):
        self.command = command
        self.cwd = cwd or None

    def send(self, payload: str, timeout: float) -> str:
        argv = self.command if sys.platform == "win32" else shlex.split(self.command)
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd, encoding="utf-8", errors="replace")
        except OSError as e:
            raise AdapterError(f"无法启动本地 Agent: {e}")
        try:
            out, err = proc.communicate(payload, timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise TimeoutError(f"本地 Agent 超时: {self.command}")
        if proc.returncode != 0:
            tail = (err or out or "").strip()[-300:]
            raise AdapterError(f"本地 Agent 退出码 {proc.returncode}: {tail}")
        result = (out or "").strip()
        if not result:
            raise AdapterError("本地 Agent 无 stdout 输出")
        return result


class ClipboardAdapter(Adapter):
    label = "clipboard"

    def __init__(self, put_clipboard, notify_wait, launch_command: str = "", on_done=None):
        """put_clipboard(text)：线程安全地把文本写入系统剪贴板（由宿主注入）。
        notify_wait(adapter, payload, token)：进入等待时通知 UI，token 标识本次等待，
        UI 回传时必须带回同一 token，防止上一轮的迟到回传串入下一轮（F6）。
        launch_command：可选，等待粘贴前尝试启动/聚焦的目标程序路径。
        on_done()：本次 send 结束（成功/超时/中止）时回调，宿主据此清等待状态。"""
        self._put = put_clipboard
        self._notify = notify_wait
        self._on_done = on_done or (lambda: None)
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
                    exe = os.path.basename(self.launch_command)
                    if not (exe.lower().endswith(".exe")
                            and focus_window_by_exe(exe)):  # 先聚焦已运行窗口（F17）
                        os.startfile(self.launch_command)  # 找不到再启动新实例
                except OSError:
                    pass  # 聚焦/启动失败不阻塞流程，剪贴板内容已就绪
            self._notify(self, payload, self._token)
            if not self._evt.wait(max(0.05, timeout)):
                raise TimeoutError("等待剪贴板回传超时")
            if self._result is None:
                raise Aborted("用户终止")
            return self._result
        finally:
            self._active = False
            try:
                self._on_done()
            except Exception:
                pass

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
