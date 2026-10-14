# 多 Agent 中转协作 App（P0）

用户向「任务布置型」Agent A 下指令，App 把 A 拆解出的结构化任务包转交给「执行型」Agent B；
B 产出回传 A 验收，不通过就循环，直到通过。A 的验收意见即终判，用户只旁观。

```
用户指令 → Agent A（出任务包） → App 结构化校验 → 脱敏 → Agent B（执行）
    ↑                                                        ↓
    └──────── Agent A 验收 ◄────── 产出回传 ◄────────────────┘
              不通过 → 重新布置 → B 再执行（循环）
              通过 → 停止，输出最终结果
```

## 运行

**首次运行前置 ①：安装 Python 依赖**（Python 3.12+，仅桌面壳需要 pywebview 一个包）：
```bash
py -m pip install -r requirements.txt
```
仅 `py server.py`（桌面窗口模式）需要 pywebview；`--browser` / `--headless` 模式、CLI
（`py app.py --demo` / `--e2e`）与自检不需要任何第三方依赖（引擎/适配器/设置层零依赖）。

**首次运行前置 ②：构建前端**。`webui_dist/` 构建产物被 .gitignore 忽略、不入 git，
克隆后本机没有，需先构建（需 Node 18+，本机安装位置见下文「前端（webui/）开发」）：
```bash
cd webui && npm install --registry=https://registry.npmmirror.com && npm run build
```
不构建则 `py server.py` 启动时会给明确报错并退出（提示同一修复命令）。构建一次后，
正常运行不再需要 Node。

**桌面应用（正式 UI，web 版）**：
```bash
py server.py          # 打开桌面窗口（pywebview + WebView2 + Vue 前端，深色主题）
py server.py --browser  # 备用：系统浏览器打开同一界面
```

**开发 / 验证 / 无界面模式**：
```bash
py test_p0.py                 # P0 全量自检
py test_p1.py                 # P1 自检（重新布置/导出脱敏/设置池/聚焦冒烟）
py app.py --demo              # 无界面端到端演示：本地 mock，3 轮循环验收通过
py app.py --e2e "指令"        # 无界面真实端到端：按 data/settings.json 的 A/B 绑定跑
py scenarios/e2e_rounds.py    # 修复 bug 场景验证：真实 LLM 验收 + 渐进补全 B，3 轮循环
py scenarios/e2e_exec.py      # 闭环执行验证：B 经 CliAdapter 真实修复 calc.py，test_calc.py 退出码 0 证实
py scenarios/webui_smoke.py   # Web UI 冒烟（headless server + API 全链路）
```

**前端（webui/）开发**：Node 18+ 与 npm（本机 zip 免安装版位于
`C:\Users\30784\AppData\Local\Programs\nodejs`，已入用户 PATH）：
```bash
cd webui && npm install --registry=https://registry.npmmirror.com
npm run dev       # 开发服务器 :5180，/api 代理到 :8977（需先起 py server.py --headless）
npm run build     # 产物输出到 ../webui_dist（py server.py 直接托管，无需 Node 常驻）
```
运行桌面版不依赖 Node——只消费 `webui_dist/` 构建产物（首次克隆需先构建一次，见上文
「运行」小节）；Node 仅在改前端时需要。

## 文件

| 文件 | 职责 |
|------|------|
| `server.py` | Web UI 宿主：stdlib HTTP API + 静态托管 webui_dist + pywebview 桌面壳（`--browser`/`--headless` 备用模式；`RELAY_PORT` 可换端口） |
| `webui/` | Vue 3 + Vite 前端源码（任务布置 / 轮次时间线 / 设置 三页，深色主题） |
| `engine.py` | 任务引擎：任务包必填校验（缺字段打回 A 补全）、脱敏、轮次/单轮超时/僵局判定、重新布置、SQLite 留痕、导出（脱敏后） |
| `adapters.py` | 适配器框架：`send(payload, timeout) -> str` + `supports()`；API 类（含 `test_connection`）+ 剪贴板半自动（含窗口聚焦）；统一异常归一 |
| `settings.py` | 设置持久化：Agent 配置池（多候选）、A/B 绑定、循环参数，存本地 `data/settings.json` |
| `app.py` | CLI 入口（`--demo` / `--e2e`；原 tkinter UI 已由 web 版替代移除） |
| `mock_agent.py` | 本地演示 Agent（OpenAI 兼容 HTTP），仅用于演示与测试，不是预设 Agent |
| `test_p0.py` / `test_p1.py` | 自检 |
| `scenarios/` | 端到端验证场景：`demo_project`（带已知 bug 的示例项目）、`e2e_rounds.py`（真实 LLM 验收 + 渐进补全 B 的 3 轮循环）、`e2e_exec.py`（闭环执行验证：B 经 CliAdapter 真实修复 calc.py）、`webui_smoke.py` |
| `requirements.txt` / `pyproject.toml` | Python 依赖声明（仅 pywebview）；安装走 `py -m pip install -r requirements.txt` |
| `data/` | 运行时生成：`relay.db`（留痕）、`settings.json`（含 key，仅本地） |

## 配置 A / B（设置页管理，App 不预设任何 Agent）

1. 打开「设置」页，在配置池中新增条目：
   - **API 类**：填 `base_url`（任何 OpenAI 兼容端点，如 `https://xxx/v1`）、`api_key`、`model`；
     「测试连接」走一次完整请求链路验证端点/key/model；
   - **剪贴板半自动**：可填目标程序路径（如 `notepad`）。App 会优先把**已在运行**的目标窗口
     置前（Windows），找不到才启动新实例；任务包写入剪贴板后你 Ctrl+V 发送，
     把对方回复粘贴回「等待回传」区点「回传给 App」。
2. 回到「任务布置」页，在 A、B 下拉框各选一个配置（同池条目可任选、可相同——即多候选
   按任务临时切换）。
3. 运行中可「终止」整个会话，或「终止本轮并重新布置」——当前轮作废（已产生数据照常留痕），
   A 重新出任务包；任务包内容本身锁定不可人工修改（需求 §9.2）。
4. 「轮次时间线」页可查任意历史会话的任意一轮（任务包原文 / 脱敏后发出 / B 产出 / 验收
   意见 / 耗时 / 阶段），并可导出 JSON——**导出内容全部重新过脱敏规则**，密钥/手机号不会带出。

注意：`api_key` 明文存于本地 `data/settings.json`（不进 git），敏感环境请自行加密盘/权限管控。

## 密钥安全

- `api_key` 明文存于本地 `data/settings.json`（已确认未进 git 历史，`data/` 整个被
  .gitignore 忽略）。
- 不要把 `data/` 移出忽略列表，也不要把它提交到任何仓库。
- 长期本地保存的密钥建议定期轮换。
- 多人共用机器时注意 `data/settings.json` 的文件权限。

## 关键机制（P0 实现范围）

- **任务包 schema**：必填 `goal / inputs / steps / acceptance / forbidden`，缺失或为空即打回 A
  补全，同一轮最多打回 3 次，仍失败则会话失败（`engine.py: validate_pack / PACK_RETRIES`）。
- **循环刹车**：默认最大 5 轮、单轮 10 分钟（UI 可调）；A 连续两次验收意见相似度 > 0.9
  （difflib）判定僵局，终止并提示重新布置。
- **任务包一轮内锁定**：引擎对 A/B 的每次调用只追加上下文，不存在中途人工改包的入口。
- **脱敏**：发给 B 的内容先过正则（API Key / token / 私钥 / Bearer / 手机号 / 身份证），
  打码命中数写入日志；A 原文与脱敏后文本分别留痕。
- **留痕**：每轮在 SQLite 记录任务包原文、脱敏后发出内容、B 产出原文、验收意见 JSON、耗时、状态
  （`data/relay.db` 的 `sessions` / `rounds` 两表）。

## 技术选型

需求建议 Tauri/Electron。P0 期本机无 Rust 与 Node 工具链，引擎/适配器/脱敏/留痕用 Python
3.12 标准库实现（此核心至今保持零第三方依赖，仅引擎层）。UI 演进三步：

1. **P0/P1 初版**：tkinter 桌面控件（当时唯一零依赖选项，视觉朴素）；
2. **现版（2026-10-13 起）**：安装 Node 22（官方 zip 免安装版，`AppData\Local\Programs\nodejs`）
   后用 **Vite + Vue 3** 重写前端，桌面壳为 **pywebview**（WebView2 渲染）——运行期仅依赖
   pywebview 一个 pip 包，桌面版不依赖 Node；tkinter 版已移除；
3. **若迁 Tauri**：引擎与适配器接口语言无关，可平移 Rust，前端代码（Vue）可直接复用。

**选型豁免登记（F22）**：该偏差为环境约束下的工程决策，需求文档 v0.1 未正式登记。
复查触发条件——满足其一时重新评估迁移 Tauri/Electron：
1. 需要打包独立安装分发给其他机器（pywebview + Python 打包体验差于 Tauri）；
2. 出现需要非 WebView 原生能力的功能（系统托盘、全局快捷键、深度窗口控制）。

## 连接本地电脑里的 Agent（三条路）

**路径一：本地 OpenAI 兼容服务（零配置）**——设置页新增 `api` 类配置，base_url 填本地服务：
- Ollama：`http://127.0.0.1:11434/v1`（key 随便填非空即可）
- LM Studio：`http://127.0.0.1:1234/v1`
- 任何自建网关（如 new-api/one-api 本地实例）

**路径二：本地命令行 Agent（`cli` 类，全自动执行）**——设置页类型选 `cli`，填两个参数：
- `command`：任务包经 **stdin** 传入、**stdout** 即产出。示例形态：
  `codex exec --skip-git-repo-check -`（Codex CLI）、`claude -p`（Claude Code）——
  具体参数以你安装的 Agent 文档为准，App 不预设任何 Agent；
- `cwd`：可选工作目录，执行型 Agent 会在该目录真实读写文件；
- ⚠ 安全：配置即授权该命令在本机执行任务，只添加你信任的 Agent；「测试连接」会真实调用一次；
- 实现要点：Windows 按 CreateProcess 规则解析命令行（引号按 Windows 习惯书写），
  POSIX 经 shlex 切分不经 shell；单轮超时自动 kill；非零退出/空 stdout 归一为适配器错误。

**路径三：剪贴板半自动（`clipboard` 类，兜底）**——GUI-only Agent 用：写入剪贴板 +
聚焦目标窗口（`launch_command` 填程序名如 `notepad`），人工粘贴、产出贴回 App。

## P0 复审登记项的处置（2026-10-13 P1 全部清偿）

- **F17 窗口聚焦**：✅ 已实现——剪贴板模式优先把已运行的目标程序窗口置前
  （`focus_window_by_exe`，ctypes EnumWindows + SetForegroundWindow，仅 Windows），
  找不到才 `os.startfile` 启动新实例。
- **F18 supports() + 可用性探测**：✅ `supports()` 已提供；设置页「测试连接」走完整请求链路。
- **F19 多候选 Agent**：✅ 设置页维护配置池，A/B 各自下拉绑定，按任务临时切换。
- **F20 终止本轮 + 重新布置**：✅ 布置页「终止本轮并重新布置」按钮；当前轮作废
  （status=replan，已产生数据留痕），下一轮 A 重新出包；任务包内容仍锁定不可人工改。
- **F21 留痕导出脱敏**：✅ 时间线页「导出会话 JSON」——task_pack 重新过 `sanitize_obj`，
  文本字段过文本脱敏，导出文件不含未脱敏敏感值。

## 端到端验证记录（2026-10-13，真实 LLM）

- `py app.py --e2e "…"`：真实 API 全链路多场验收通过（GLM-flash / deepseek-chat 均一轮
  交付完整——强模型对结构化任务包一次做对，循环未触发，属正常）。
- `py scenarios/e2e_rounds.py`：**修复 bug 场景 3 轮循环验收通过**——A 为真实 LLM
  （逐项验收、两轮打回并明确指出缺项），B 为渐进补全执行代理（模拟不完美执行者，
  1→2→4→5 项逐轮补齐），完整走通 打回→补全→再验收 循环，会话留痕可查。

## 本地执行与闭环执行验证（原「P1 之后的远期」，已实现）

原登记的两项远期方向均已成为现状，不再是「后续」：

- **CLI 进程适配器已实现**：`cli` 类适配器（`adapters.py` 的 `CliAdapter`）——任务包经
  stdin 传入本地命令行 Agent（如 `codex exec` / `claude -p`），stdout 即产出，全自动执行；
  B 可在其 `cwd` 工作目录里真实读写文件。配置方法见上文「连接本地电脑里的 Agent」路径二。
- **闭环执行验证已实现**：任务包可选 `verify` 字段（A 声明验证命令）——引擎在 B 产出后
  真实执行该命令，退出码 / stdout / stderr 作为验收证据注入 A；执行失败（验证命令未通过）
  时 A 不得判通过。`py scenarios/e2e_exec.py` 演示：B 经 CliAdapter 真实修复
  `demo_project/calc.py`，并由 `test_calc.py` 退出码 0 证实。
