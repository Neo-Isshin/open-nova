# Actanara 本地操作 Runbook

<p align="center">
  <img src="https://img.shields.io/badge/Language-简体中文%20·%20当前-C026D3?style=for-the-badge" alt="当前语言：简体中文">
  <a href="local-operations-runbook.md"><img src="https://img.shields.io/badge/Language-English-2563EB?style=for-the-badge" alt="Switch to English Runbook"></a>
</p>

**简体中文（当前）** · [English](local-operations-runbook.md) · [返回中文 README](../README.zh-CN.md)

状态：公开用户操作指南<br>
适用范围：Actanara · macOS 与 Linux 本地 Runtime

## 1. 本指南解决什么问题

README 负责解释产品和快速开始；本 Runbook 负责从安装前检查到首次配置、历史回填、日常 Pipeline、Dashboard、Nova-Task、`nova-RAG`、更新、备份和故障排查的完整操作路径。

文中的 **Agent Runtime** 指拥有独立会话、日志、记忆和执行上下文的 AI 工具环境，例如 Codex、Claude Code、Gemini CLI、OpenClaw、Hermes、OpenCode、Antigravity 和 Cursor。

## 2. 版本与发布边界

- GitHub 是唯一公开发布与安装来源；任何私有开发归档都不属于公开安装链路。
- 已发布的 tag、Release 和 artifacts 保持不可变。
- 公开 one-liner 从最新稳定且不可变的 Release 下载 POSIX setup 入口；该产物已固定到 Release 的精确完整源码 commit，并从同一 commit 选择平台适配器。
- `v1.0.0` 已撤回，只保留用于审计，不应继续安装或推荐。

> [!IMPORTANT]
> 选中的源码 commit 携带适用于受支持 Python ABI、平台与架构的精确 Runtime dependency lock；每次安装事务都会固定到该 commit。

## 3. 安装前检查

确认以下条件：

- 🍎 macOS 上，`zsh`、`git` 和 `curl` 已在 `PATH` 中；
- 🐧 Linux 上，POSIX `sh`、`git`、`curl`、CPython 3.13 与可工作的 `systemctl --user` manager 已就绪；
- 🐍 受支持的 Apple Silicon 与 Intel Mac 需要 Python `>=3.11`；缺少兼容版本时，安装器可以下载并校验托管 Python；
- 🌐 可以访问 GitHub、Python 包索引和所选 LLM / Embedding Provider；
- 💾 本地有足够空间容纳 Runtime、Dashboard 和可选的本地 Embedding 依赖；
- 🔐 计划使用的 Provider API Key 已准备好，但不要先写入 Shell 历史、README 或普通配置文件。

检查基础工具：

```bash
command -v zsh
command -v sh
command -v git
command -v curl
python3 --version 2>/dev/null || true
systemctl --user is-system-running 2>/dev/null || true
```

macOS 保留现有引导式设置。Linux 使用非交互模式，支持全新安装、受保护的
更新/修复，以及 cloud 或 CPU-only 本地 Embedding RAG。Linux 的 x86_64 与
arm64 使用独立锁目标，但不维护两套应用实现；受保护更新/修复门禁已在
Debian x86_64、CPython 3.13 上验证。

## 4. 从最新稳定 Release 安装或刷新

使用公开 one-liner：

```bash
curl -fsSL https://github.com/Neo-Isshin/actanara/releases/latest/download/install.sh | sh
```

这条命令在隐藏源码获取细节的同时固定实际安装版本：

1. GitHub 从最新稳定且不可变的 Release 提供 POSIX setup 入口；
2. Release 构建器把入口固定到该 Release 的完整源码 commit；
3. 从同一 commit 获取 macOS 或 Linux 适配器；
4. 适配器安装相同的 detached commit。

> [!NOTE]
> 公开安装跟随不可变 Release tag，而不是移动中的开发分支。高级与离线场景仍可显式使用 `--source-root` 或完整 `--ref`。

> [!WARNING]
> 两个平台上的同一条命令都支持全新和已有 Runtime，并保留用户 Settings、
> 数据库、密钥、日志和生成资产。Linux 在受管理 systemd 定义或依赖 profile
> 证据与所选 Runtime 不一致时，会在停止服务前保守失败。

macOS 安装向导会依次处理：

1. 界面语言与 Pipeline 语言；
2. 外部 Agent Runtime 路径；
3. LLM Provider、Endpoint、Model 和 API Key；
4. 是否启用 `nova-RAG`；
5. 本地或云端 Embedding 配置；
6. macOS Dashboard 与调度服务。

Linux 全新安装使用默认值，也可在 `--` 后传入选项。Dashboard 与调度服务
安装为用户级 systemd unit。公开入口选中已有 managed Runtime 时，会先展示
固定到精确 commit 的升级计划，并通过控制终端请求确认；没有控制终端时以
状态码 2 退出且不改动 Runtime，而是输出固定到所选源码 URL、commit、
Runtime 与 cache 的 `actanara update --dry-run` 和
`actanara update --apply` 命令。全新安装存在
控制终端时，安装器还会在请求当前用户的 linger 前明确询问，且绝不调用
`sudo`；非交互全新安装默认保持 linger 现状，除非明确使用
`--enable-linger` 或 `--require-linger`。

公开安装 locale 使用 `zh-CN` 或 `en-US`；Runtime 内部 Pipeline profile 使用 `zh` 或 `en`。用户通常只需在安装向导中选择语言，不应手工混写这两组值。

## 5. 安装器会写入哪些位置

| 路径 | 用途 |
| :--- | :--- |
| `~/.cache/actanara/installer` | 安装源码缓存 |
| `~/.actanara` | Runtime、虚拟环境、设置、数据库、日志、密钥和生成资产 |
| `~/.config/actanara/location.json` | 当前活动 Runtime 指针 |
| `~/.local/bin/actanara` | 面向用户 `PATH` 的 CLI 入口 |
| `~/.zprofile` | 仅 macOS：带标记的 `PATH` 配置；可用 `--no-shell-path` 禁用 |
| `~/Desktop/Actanara` | 仅 macOS：默认指向日记目录的桌面快捷链接 |
| `~/Library/LaunchAgents/` | 仅 macOS：Dashboard、Scheduler 和可选 RAG 服务 |
| `~/.config/systemd/user/` | 仅 Linux：Dashboard、Scheduler 和可选 RAG 用户 unit |

Provider Key 保存到 `$ACTANARA_HOME/state/secrets`。密钥目录权限为 `0700`，密钥文件权限为 `0600`。

旧的 `macos-keychain` 引用只用于兼容迁移：能读取时会复制到 Runtime Secret Store，但 Actanara 不会自动删除旧 Keychain item。若旧密钥不可读，请在 Dashboard 中重新输入。

## 6. 安装摘要与基础验证

安装完成后保留终端摘要，其中包含实际 Dashboard URL、Runtime 位置和常用命令。

默认 Dashboard 地址：

```text
http://127.0.0.1:3036/dashboard
```

如果 `3036` 已被占用，安装器会选择其他端口；始终以安装摘要和当前 Runtime 设置为准。

先运行以下只读命令：

```bash
actanara doctor
actanara model show
actanara onboard status
actanara config show
```

按子系统检查：

```bash
actanara doctor --installer
actanara doctor --pipeline
actanara doctor --scheduler
actanara doctor --rag
```

Warning 不一定阻断运行；Error 或明确的 readiness failure 应先处理。重点检查 Runtime 指针、Provider、Dashboard、调度器和可选 RAG Server。

## 7. 配置 LLM Provider

1. 打开安装摘要中的 Dashboard URL；
2. 进入 LLM Provider 设置；
3. 选择 Provider，确认 Endpoint 和 Model；
4. 输入 API Key；
5. 运行可用性检测；
6. 检测通过后保存。

随后运行：

```bash
actanara model show
actanara doctor --pipeline
```

不要把真实 Key 写入 README、Issue、日志、Shell 历史、Git 文件或普通 settings 字段。若配置外部 LLM 或云端 Embedding Provider，相关派生工作内容会按所选 Endpoint 和 Provider 数据政策发送。

## 8. 首次历史数据生成

### 8.1 计划预览

在 Dashboard 点击“生成历史数据”，选择日期范围并先运行计划预览。检查：

- 待生成的每日记录；
- 周报与月报；
- 已存在并将跳过或重新生成的产物；
- 预计 LLM 调用；
- Nova-Task 任务；
- 启用时的 `nova-RAG` 同步任务。

取消不需要的项目。计划预览本身不写入 Runtime。

### 8.2 排队与监控

确认后将勾选任务加入后台队列。较长日期范围会运行更久；没有活动的日期可能只生成结构化占位产物，不一定调用 LLM。

在“后台任务”和“消息”中查看状态。运行任务可以请求取消；部分失败的回填任务可以只重试失败项。一个 Runtime 同时只应保留一个活动历史回填任务。

完成后检查：

- 日记与周期报告；
- AI 资产；
- Nova-Task 看板；
- 启用时的 `nova-RAG` 状态与搜索结果。

## 9. Base Pipeline 日常运行

手动运行：

```bash
actanara pipeline
actanara pipeline YYYY-MM-DD
```

不指定日期时，`actanara pipeline` 处理**当前配置时区的前一个日历日**；它不是“处理今天”的快捷方式。指定日期时使用 `YYYY-MM-DD`。

Pipeline 会写入日记、报告和 Foundation 数据。如果目标日期已完整生成，只有明确使用 `--force` 才会基于冻结的 Foundation 输入重新生成。

Actanara 根据外部 Runtime 的执行证据进行工作区归因，不把启动 CLI 时的当前目录当作唯一依据。

检查托管调度：

```bash
actanara doctor --scheduler
```

若改由外部自动化调用 Pipeline，应避免与安装器注册的托管调度重复执行。

## 10. Dashboard 日常操作

Dashboard 用于：

- 查看每日、每周和每月日记；
- 查看实时用量、AI 资产与工作区归因；
- 配置 Provider、外部工具路径和调度；
- 规划历史回填；
- 监控后台任务与消息；
- 审阅 Nova-Task；
- 管理可选的 `nova-RAG`。

服务异常时：

```bash
actanara dashboard restart
actanara doctor --installer
```

不要默认认为 Dashboard 一定使用 `3036`；先查看安装摘要或 `actanara config show`。

## 11. Nova-Task 操作

在 Dashboard 中打开任务看板。CLI 的 `task` 命令只读取并输出任务统计，不会打开界面：

```bash
actanara task
actanara task --json
```

Nova-Task 是 Beta 子系统，根据真实 Runtime 活动、对话、文件变化、工具结果和执行证据生成任务结构。

操作原则：

- 一级或高影响任务保留人工审阅；
- 普通子任务可以由系统维护，但应定期检查状态和归属；
- 导入 RFC、PRD、Roadmap 或 Audit 前，确认内容适合进入本地任务图谱；
- 任务图谱描述实际发生的工作，不等同于传统的手工待办清单。

## 12. nova-RAG 操作

检查状态：

```bash
actanara doctor --rag
```

搜索本地记忆：

```bash
actanara search "deployment issue" --top-k 5
actanara search "deployment issue" --top-k 5 --json
```

自动化脚本使用 JSON 输出时，应检查返回的 `available` 字段；RAG 暂不可用时，命令仍可能成功返回结构化状态。

先预览维护操作：

```bash
actanara rag-update --dry-run
actanara rag-rebuild --dry-run
```

外部 Agent Runtime 应优先使用 Dashboard 的只读 Facade：

```text
GET  /api/rag/external/health
GET  /api/rag/external/stats
GET  /api/rag/external/contract
POST /api/rag/external/search
```

默认 Dashboard 基址为 `http://127.0.0.1:3036`，直接 RAG Server 默认为 `http://127.0.0.1:3037`。实际端口以 Runtime 设置为准。

外部 Runtime 合约只允许健康检查、统计、合约读取和搜索；不允许写入记忆、修改索引、变更全局设置或控制服务生命周期。

## 13. 更新

> [!IMPORTANT]
> macOS 保留既有 launchd 事务。Linux 通过 POSIX 适配器使用同一更新命令，
> 精确保留 systemd enabled/active 状态，并在停止服务前拒绝定义漂移。可信
> Linux Runtime 损坏时应使用需确认的 repair，而不是常规 update。

查看更新计划：

```bash
actanara update
```

无变更预演：

```bash
actanara update --dry-run
```

应用受保护更新：

```bash
actanara update --apply
```

- 无参数只显示计划；
- `--dry-run` 会运行 bootstrap 和安装器预演，并说明复用现有 venv 还是按锁重建；远端源码冷缓存时仍可能只能展示源码获取计划；
- 只有 `--apply` 会执行真实更新事务。

安装器与 updater 使用所选 Release 或源码 commit 中相同的 dependency contract 与精确 Runtime lock。默认更新会在依赖、Python ABI、启用 profile 与 venv 内实际安装**完全一致**时复用 active venv（只切换源码指针，不运行 pip）；否则从带 hash 校验的 lock 构建独立候选 venv，验证通过才原子切换，绝不在 active venv 中原地安装。证据缺失或不明确时保守失败，不会冒险猜测依赖选择。

```bash
actanara update --apply --offline --ref <full-commit-sha>        # 使用已缓存远端 commit
actanara update --apply --offline --source-root /path/to/source  # 使用本地 checkout
actanara update --apply --source-only                            # 必须复用 venv，否则 fail closed
actanara update --apply --force-rebuild                          # 必须创建按锁构建的新 candidate venv
```

从选定 checkout 修复可信 Linux Runtime：

```bash
sh install/setup.sh --source-root /path/to/source -- \
  --runtime /path/to/runtime --repair-existing --yes --no-linger-prompt
```

Linux repair 只 reconcile Actanara 管理的 unit 文件；不会调用 `sudo`、改变
linger 或删除用户自有 unit。

离线模式只接受已存在于 installer 缓存中的完整 commit 或本地 `--source-root`，绝不解析 `latest`。

指定不可变完整 commit：

```bash
actanara update --dry-run --ref <full-commit-sha>
actanara update --apply --ref <full-commit-sha>
```

Linux 上显式选择 `--source-url`/`--ref` 时，bootstrap 旁边的 checkout
不会成为源码；bootstrap 只负责启动执行。installer cache 的规范化
`origin` 必须与请求一致，在线 fetch 与离线 cache 复用也都必须解析并部署
精确 commit。

更新前：

1. 确认 Pipeline 和后台任务已结束；
2. 保存 `actanara doctor` 输出；
3. 备份 Runtime 设置和重要生成资产；
4. 记录当前 Runtime 与 commit；
5. 先运行计划或 Dry Run。

## 14. 日志与故障排查

优先检查：

```text
~/.actanara/state/logs/
~/.actanara/config/settings.json
~/.config/actanara/location.json
```

建议按以下顺序排查：

1. Runtime 指针是否正确；
2. Dashboard URL 和端口是否与摘要一致；
3. LLM Provider 检测是否通过；
4. `$ACTANARA_HOME/state/secrets` 是否为 `0700`，密钥文件是否为 `0600`；
5. Pipeline 与平台服务管理器（macOS LaunchAgent 或 Linux systemd user unit）是否使用同一个 `ACTANARA_HOME`；
6. 外部工具路径是否存在且已启用；
7. Scheduler 是否重复、缺失或失败；
8. RAG Server 与活动索引是否就绪；
9. 后台任务是否失败、取消或可重试。

提交 Issue 时保留命令、必要输出、日志路径、Runtime 指针和相关 Doctor 结果；删除密钥、邮箱、用户名、私人项目名、本机路径和工作内容。

## 15. 数据、备份与隐私

- 不提交 Runtime 数据库、日志、缓存、密钥、生成日记或索引；
- 截图公开前检查邮箱、用户名、项目名、本机路径、Token / RAG 指标和工作内容；
- 对外发布截图时，优先使用一次性隔离 Runtime 和完全合成的数据；
- 外部 LLM / Embedding 请求由用户配置的 Provider 处理；
- 定期备份重要日记、报告、Nova-Task 数据和 Runtime 设置；
- 删除或迁移 Runtime 前先停止托管服务，并确认备份可恢复。

## 16. 卸载边界

Actanara 当前没有产品级一键卸载器。不要只删除 `~/.actanara`，否则可能遗留：

- macOS LaunchAgent 或 Linux systemd user unit；
- `~/.local/bin/actanara` CLI shim；
- Runtime location pointer；
- `~/.zprofile` 中的带标记 `PATH` 区块；
- 桌面快捷链接；
- 安装源码缓存。

在公开提供经过验证的卸载流程前，建议保留 Runtime 或先进行完整备份，再逐项审阅安装摘要中的写入位置。

## 17. 完成检查清单

- [ ] Dashboard 可访问，URL 与安装摘要一致；
- [ ] LLM Provider 检测通过；
- [ ] `actanara doctor` 无阻断错误；
- [ ] Scheduler 状态符合预期；
- [ ] 首次历史回填完成或处于可观察状态；
- [ ] 日记、AI 资产和 Nova-Task 有预期数据；
- [ ] 启用 `nova-RAG` 时，Server、活动索引和搜索正常；
- [ ] 已了解日志、更新、备份和卸载边界。

## 18. 相关参考

- [中文 README](../README.zh-CN.md)
- [中文新用户指南](new-user-onboarding-runbook.zh-CN.md)
- [CLI 产品边界（English）](cli-boundary.md)
- [nova-RAG 外部 Agent 合约（English）](rag-external-agent-contract.md)
- [GitHub Releases](https://github.com/Neo-Isshin/actanara/releases)
