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
py test_p0.py           # P0 全量自检（校验/脱敏/相似度/引擎全流程/僵局/超时/留痕）
py app.py --demo        # 无界面端到端演示：本地 mock，3 轮循环验收通过
py app.py               # 打开演示控制台 UI
py app.py --ui-selftest # UI 自动自检（跑一次 mock 会话并校验结果）
```

## 文件

| 文件 | 职责 |
|------|------|
| `engine.py` | 任务引擎：任务包必填校验（缺字段打回 A 补全）、脱敏、轮次/单轮超时/僵局判定、SQLite 留痕 |
| `adapters.py` | 适配器框架：`send(payload, timeout) -> str`；API 类 + 剪贴板半自动 |
| `mock_agent.py` | 本地演示 Agent（OpenAI 兼容 HTTP），仅用于演示/测试，不是预设 Agent |
| `app.py` | 演示控制台 UI + 无头 demo |
| `test_p0.py` | P0 自检 |
| `data/relay.db` | 运行时生成的 SQLite 留痕库 |

## 配置 A / B（UI 内选择，App 不预设任何 Agent）

每个角色三选一：

1. **本地Mock(API)** — 连接 App 内置的本地 mock（OpenAI 兼容端点），用于演示与联调；
2. **剪贴板半自动** — App 把任务包写入系统剪贴板（可选自动启动目标程序，如填记事本路径），
   你切换到目标 Agent 窗口 Ctrl+V 发送，再把对方回复粘贴回 App 的「等待回传」区点「回传给 App」；
   同一适配器对 A、B 角色通用（A 出包和 A 验收同样可走剪贴板）；
3. **自定义API** — 任何 OpenAI 兼容端点：填 `base_url`（如 `https://your-agent.example.com/v1`）、
   `api_key`、`model`。App 只存本地，不硬编码任何厂商地址或密钥。

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

## P0 复审登记的未交付项（2026-10-13，F17-F23）

以下为需求文档要求但 P0 未实现的能力，登记待 P1 处理，非遗漏：

- **F17 窗口聚焦**：需求 §5.3「复制/聚焦/回贴」中的「聚焦」仅实现 `os.startfile`
  启动目标程序（adapters.py），未实现把**已运行**窗口置前（需 Win32
  `SetForegroundWindow`/ctypes 或 macOS Accessibility）。P0 行为：启动新实例或聚焦失败
  时静默继续，剪贴板内容已就绪不受影响。
- **F18 supports()**：已提供最小实现（两类适配器均可担任 A/B）；可用性探测（API 连通性
  ping 等）与多候选切换留待 P1。
- **F19 多候选 Agent**：需求 §6「同一角色可配置多个候选 Agent」未预留数据结构；
  当前每角色一个适配器实例，且 UI 的自定义 API 参数为 A/B 共用一组，同选自定义 API
  时需先改参数再切换。P1 正式设置页一并做。
- **F20 终止本轮 + 重新布置**：需求 §9.2 的「终止本轮重新布置」当前只有整体终止
  （stop 后整个会话 aborted），无「保留会话、仅重开一轮」的入口。
- **F21 留痕导出脱敏**：rounds.task_pack 按需求 §5.5 存 A 的**未脱敏原文**（含
  sent_to_b 脱敏后版本，两列并存）。P1 做导出 JSON 时必须**导出前重新过脱敏规则**，
  否则密钥/手机号将原样带出。

## P1（未做，按需求文档顺延）

正式 UI（布置页/轮次时间线/设置页）、记录导出 JSON（见 F21 注意事项）、真实场景端到端
（A 出包 → 真实 B 修复 bug → A 验收）、窗口聚焦与多候选（见上文 F17/F19）。
