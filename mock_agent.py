"""演示用本地 mock Agent（纯本地，不涉及任何真实 Agent、密钥或外网）。

实现 OpenAI 兼容的 POST /v1/chat/completions：
- 路径前缀 /a/ 扮演 Agent A：出任务包、验收（前 2 轮不通过且意见不同，第 3 轮通过）；
- 路径前缀 /b/ 扮演 Agent B：返回修复产出。
这样演示走的是 ApiAdapter 的真实 HTTP 代码路径，而非绕过适配器。
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PACK = {
    "goal": "修复 demo_project 登录后头像不刷新的 bug (#12)",
    "inputs": [
        "bug 报告：用户登录成功后，导航栏头像仍显示上一位登录用户的头像",
        "相关模块：src/auth/session.js、src/components/Avatar.jsx",
        "联系上下文请粘贴到聊天框，示例 token: sk-demo1234567890abcdef 用于测试脱敏",
    ],
    "steps": [
        "复现：同一浏览器先用账号 A 登录，退出后换账号 B 登录",
        "定位 session.js 中用户状态未清空的分支",
        "修复并让 Avatar.jsx 订阅用户状态变更",
        "补一条回归测试覆盖连续切换账号场景",
    ],
    "acceptance": [
        "切换账号后头像立即更新",
        "既有登录测试全部通过",
        "新增回归测试通过且覆盖连续两次切换账号",
    ],
    "forbidden": ["不引入新依赖", "不改动无关文件", "不删除既有测试"],
}

B_OUTPUT_ROUND = {
    1: "已修复。变更：\n1. session.js：logout() 补 user=null\n2. Avatar.jsx：改为订阅 session.user\n3. 新增回归测试 test/avatar-refresh.test.js（仅覆盖单次切换账号）\nnpm test：42 通过 0 失败。",
    2: "已按意见补充。变更：\n1. 回归测试增加『连续两次切换账号』用例（通过）\n2. session.js：登出时同时清理 localStorage 中的缓存头像\nnpm test：43 通过 0 失败。",
    3: "已补充。变更：\n1. session.js：登出时清理 localStorage 缓存头像与 sessionStorage 会话残留\n2. 回归测试覆盖连续切换、刷新页面两个场景\nnpm test：45 通过 0 失败。",
}

VERDICTS = [
    (False, "回归测试写得不完整：未覆盖『同一浏览器连续两次切换账号』的场景，请补充该用例。"),
    (False, "新用例已通过，但缓存清理不彻底：只清了内存态，localStorage 残留仍有旧头像风险。"),
    (True, "三条验收标准逐条核对通过：头像即时更新、既有测试通过、回归测试覆盖完整。验收通过。"),
]


class _State:
    def __init__(self):
        self.n_review = 0
        self.round = 1
        self.lock = threading.Lock()


def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = body["messages"][0]["content"]
            is_a = self.path.startswith("/a/")
            content = self._act_a(prompt) if is_a else self._act_b()
            resp = json.dumps({"id": "mock", "object": "chat.completion", "model": body.get("model", ""),
                               "choices": [{"message": {"role": "assistant", "content": content},
                                            "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def _act_a(self, prompt):
            if prompt.startswith("你是任务布置型 Agent A，现在验收"):
                with state.lock:
                    i = min(state.n_review, len(VERDICTS) - 1)
                    state.n_review += 1
                    passed, comment = VERDICTS[i]
                return f'```json\n{{"passed": {str(passed).lower()}, "comments": "{comment}"}}\n```'
            # 出任务包：每轮首次故意缺 forbidden，演示「缺字段打回 A 补全」
            if re.search(r"你上次的输出不合法", prompt):
                pack = dict(PACK)
            else:
                pack = {k: v for k, v in PACK.items() if k != "forbidden"}
            return "```json\n" + json.dumps(pack, ensure_ascii=False, indent=2) + "\n```"

        def _act_b(self):
            with state.lock:
                out = B_OUTPUT_ROUND[min(state.round, 3)]
                state.round += 1
            return out

    return Handler


def start_mock():
    """启动本地 mock，返回 (a_base_url, b_base_url, stop)。"""
    state = _State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    stop = srv.shutdown
    return f"http://127.0.0.1:{port}/a/v1", f"http://127.0.0.1:{port}/b/v1", stop
