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

## 运行（本机 Python 3.12，零第三方依赖）

```bash
py test_p0.py                 # P0 全量自检
py test_p1.py                 # P1 自检（重新布置/导出脱敏/设置池/聚焦冒烟）
py app.py                     # 打开正式 UI（布置 / 时间线 / 设置 三页）
py app.py --demo              # 无界面端到端演示：本地 mock，3 轮循环验收通过
py app.py --ui-selftest       # UI 自动自检（跑一次 mock 会话并校验结果）
py app.py --e2e "指令"        # 无界面真实端到端：按 data/settings.json 的 A/B 绑定跑
py scenarios/e2e_rounds.py    # 修复 bug 场景验证：真实 LLM 验收 + 渐进补全 B，3 轮循环
```

## 文件

| 文件 | 职责 |
|------|------|
| `engine.py` | 任务引擎：任务包必填校验（缺字段打回 A 补全）、脱敏、轮次/单轮超时/僵局判定、重新布置、SQLite 留痕、导出（脱敏后） |
| `adapters.py` | 适配器框架：`send(payload, timeout) -> str` + `supports()`；API 类（含 `test_connection`）+ 剪贴板半自动（含窗口聚焦）；统一异常归一 |
| `settings.py` | 设置持久化：Agent 配置池（多候选）、A/B 绑定、循环参数，存本地 `data/settings.json` |
| `app.py` | 正式 UI 三页（任务布置 / 轮次时间线 / 设置）+ `--demo` / `--e2e` / `--ui-selftest` 模式 |
| `mock_agent.py` | 本地演示 Agent（OpenAI 兼容 HTTP），仅用于演示与测试，不是预设 Agent |
| `test_p0.py` / `test_p1.py` | 自检 |
| `scenarios/` | 端到端验证场景：`demo_project`（带已知 bug 的示例项目）、`e2e_rounds.py`（真实 LLM 验收 + 渐进补全 B 的 3 轮循环场景） |
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

需求建议 Tauri/Electron，但本机无 Rust 与 Node 工具链（两者均需 GB 级安装，无法当日交付演示），
而 P0 的本质——引擎、适配器、脱敏、留痕——是纯逻辑，Python 3.12 标准库
（tkinter/sqlite3/urllib/difflib/re）零依赖全覆盖，故 P0 用 Python 实现。
引擎与适配器接口与语言无关，若后续确认走 Tauri，此逻辑可直接平移到 Rust。

**选型豁免登记（F22）**：该偏差为环境约束下的工程决策，需求文档 v0.1 未正式登记。
复查触发条件——满足其一时重新评估迁移 Tauri/Electron：
1. 本机可用 Rust（cargo）或 Node（npm）工具链；
2. 需要打包独立安装分发给其他机器（Python 打包体验明显差于 Tauri）；
3. 出现需要非 Python 原生能力的功能（系统托盘、全局快捷键、深度窗口控制）。

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

## P1 之后的远期（不在当前范围）

真实 Agent 端到端仍受限于「B 无法本地执行代码」——产出是代码文本而非可运行验证；
接入可本地执行的 Agent 端点（如 CLI Agent 的进程适配器）是后续方向。
