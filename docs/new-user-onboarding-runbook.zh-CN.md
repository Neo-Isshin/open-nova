# Actanara 安装指南

<p align="center">
  <img src="https://img.shields.io/badge/Language-简体中文%20·%20当前-C026D3?style=for-the-badge" alt="当前语言：简体中文">
  <a href="new-user-onboarding-runbook.md"><img src="https://img.shields.io/badge/Language-English-2563EB?style=for-the-badge" alt="Switch to English onboarding guide"></a>
</p>

**简体中文（当前）** · [English](new-user-onboarding-runbook.md) · [返回中文 README](../README.zh-CN.md)

状态：公开新用户安装指南<br>
适用范围：Actanara · macOS 与 Linux 本地 Runtime

本指南覆盖全新安装、首次健康检查与受支持的更新。[`Neo-Isshin/actanara`](https://github.com/Neo-Isshin/actanara) 的 GitHub Releases 是公开安装与更新的权威来源。

## 环境要求

- macOS 使用 `zsh`，Linux 使用 POSIX `sh` 与用户级 systemd manager；
- 两个平台都需要 `git` 与 `curl`；
- macOS 需要 Python 3.11 或更新版本；当前经审计的 Linux lock 面向
  CPython 3.13；
- 安装期间可访问 GitHub 与 Python 包索引；
- 一个可在自己 home 目录下创建文件的本地用户账号；
- 如果日记生成会调用外部 Provider，需要 LLM Provider 的 Endpoint 与凭据。

默认 Runtime 为 `~/.actanara`。Actanara 把应用版本、当前虚拟环境、Settings、SQLite 数据、日志、生成资产和回滚状态都放在该 Runtime 下。安装器把当前 Runtime 记录在 `~/.config/actanara/location.json`。

## 一行安装或刷新

运行公开安装器：

```bash
curl -fsSL https://raw.githubusercontent.com/Neo-Isshin/actanara/main/install/setup.sh | sh
```

GitHub 从 `main` 提供持续维护的 POSIX 入口。它把官方 `origin/main`
解析为完整 commit，从同一 commit 获取平台适配器，再调用既有 macOS
安装器或 Linux 安装器。

macOS 入口同时支持新建和已有 Runtime，并保留原有更新/修复事务。Linux
支持全新安装、仅源码刷新、自动或强制按锁重建 venv，以及经明确确认的
repair。两个平台都会保留用户 Settings 与数据；Linux 还会逐个保持受管理
systemd unit 原有 enabled/active 状态。

已有 managed Linux Runtime 时，交互执行会先预演固定到精确 commit 的升级
并询问是否应用；非交互执行以状态码 2 退出且不改动 Runtime，而是输出固定
参数的 `actanara update --dry-run` 与 `actanara update --apply` 命令。显式选择远端
源码时，installer cache 的 `origin` 和请求 commit 必须匹配，bootstrap
旁边的 checkout 不会被替换成实际源码。

## 从本地 checkout 安装

只预览计划、不写入：

```bash
sh install/setup.sh --dry-run
```

从当前 checkout 安装：

```bash
sh install/setup.sh
```

`pyproject.toml` 是直接的依赖/profile 合约，而 `install/runtime-dependencies.lock.json` 是每个受支持 Python ABI、平台与架构的精确 Runtime 解析权威。全新安装与候选 venv 重建都使用同一份 wheel-only、SHA-256 校验的 lock。`requirements-release.txt` 只锁定发布构建工具，不是 Runtime 依赖锁。常规安装包含 Base-Pipeline、Dashboard 和 Nova-Task；nova-RAG 是可选的。开发者测试依赖按需启用，从不属于默认生产 profile。

常用的非交互控制参数：

```bash
sh install/setup.sh -- --enable-rag
sh install/setup.sh -- --no-scheduler
sh install/setup.sh -- --no-dashboard-server
sh install/setup.sh -- --runtime /path/to/runtime
sh install/setup.sh -- --no-shell-path
```

引导式 `--no-wizard` 与 `--shell-path-file /path/to/profile` 仅适用于
macOS。Linux 不编辑 Shell profile；不加 `--no-shell-path` 时只创建
`~/.local/bin/actanara` 链接。选择 Linux 托管服务时，交互安装会询问是否
需要退出登录后继续运行。`--enable-linger` 明确允许发起不含 `sudo` 的
`loginctl` 请求，`--require-linger` 会在 Runtime 写入前要求 linger 已启用，
`--no-linger-prompt` 保持现状；`--yes` 永远不代表 linger 授权。

安装器在改动 Runtime 前会执行 preflight：校验路径、Python 兼容性、源码洁净度、端口策略、冲突检查和所选依赖组。preflight 失败会终止事务。

## 引导式选择

macOS 交互式安装器只会询问所选产品 profile 需要的选项：

- 界面语言（`zh-CN` 或 `en-US`）；
- 是否启用 nova-RAG；
- 启用 nova-RAG 时的本地或云端 Embedding 模式；
- LLM Provider、Endpoint、Model 和凭据引用；
- 托管的 Dashboard、nova-RAG 和调度服务选择；
- 可选的外部工具覆盖范围与桌面日记快捷方式。

Provider 凭据只写入 Runtime 内的私有密钥存储，权限严格。Settings 只包含引用和 Provider 元数据，不含原始密钥。不要把真实凭据放进仓库，或会留在 Shell 历史的命令里。

## CLI 与 Shell 路径

Runtime 命令 shim 为：

```text
~/.actanara/bin/actanara
```

安装器还会尝试在 `~/.local/bin/actanara` 建立链接，并在所选 Shell profile 中写入受管理的 PATH 区块。用 `--no-shell-path` 跳过该 profile 编辑，或用 `--shell-path-file /path/to/profile` 指定文件。

开一个新 Shell，然后验证：

```bash
actanara doctor
actanara onboard status
actanara model show
actanara config show
```

macOS 安装器在宣布成功前还会运行 post-install doctor。任何阻断性结果都会保留前一个可用源码与 venv 以便恢复。

Linux 安装器会显式初始化 Settings 与全部 SQLite migration，再用
`systemctl --user` 注册所选 Dashboard 和调度 unit。只有得到明确授权后
才会修改 linger，并且绝不调用 `sudo`。若主机要求管理员权限，请单独执行
`sudo loginctl enable-linger "$USER"` 后重试。卸载 Actanara 时不会关闭
linger，因为其他用户服务也可能依赖它。

## Dashboard 与 nova-RAG

Dashboard 通常监听 loopback。优先使用端口 `3036`，被占用时安全回退到其他端口。安装器在完成摘要中打印选中的 URL。

启用 nova-RAG 本地模式时，其服务通常使用 loopback 端口 `3037`。
Linux 本地 Embedding 使用经过审计的 CPU-only PyTorch wheel，同时仍支持 cloud/server RAG。外部 Agent
集成必须使用 [rag-external-agent-contract.md](rag-external-agent-contract.md) 描述的只读 API。

Actanara 默认不把这些服务暴露到公网。除非你单独配置了经过认证的私有网络访问，否则请保持 loopback 绑定。

## 稳定更新

预览更新：

```bash
actanara update --dry-run
```

应用最新稳定 Release：

```bash
actanara update --apply
```

Actanara 只会更新到 tag 解析为完整 commit 的稳定 GitHub Release；不会静默回退到其他主机或 `main`。依赖不变时复用现有 venv；否则从带 hash 校验的 lock 重建并验证后再切换。Settings、SQLite、日志、生成资产、服务配置和回滚元数据都会保留；激活失败时会恢复前一个源码、venv 和服务状态。

改为从本地 checkout 升级：

```bash
zsh install/install.sh --upgrade --runtime /path/to/runtime --source-root "$PWD"
```

离线、指定 commit 或强制重建更新，见[中文本地操作 Runbook](local-operations-runbook.zh-CN.md) 第 13 节「更新」。

离线更新必须显式选择不可变来源：

```bash
actanara update --apply --offline --ref <full-commit-sha>
actanara update --apply --offline --source-root /path/to/source
```

以上更新流程会在 macOS 选择 launchd、在 Linux 选择 systemd user service。
Linux 常规更新要求 Actanara 管理的定义已经对齐；inventory 漂移时会在停止
服务前失败。对于可信但损坏的 Linux Runtime，可从选定源码 checkout 执行需
明确确认的 repair：

```bash
sh install/setup.sh --source-root "$PWD" -- \
  --runtime /path/to/runtime --repair-existing --yes --no-linger-prompt
```

repair 不调用 `sudo`、不改变 linger，并拒绝非 Actanara 管理的 unit 定义。

## 备份与恢复

重大更新前，备份重要的生成日记、报告、Runtime Settings 和 SQLite 数据。不要在未处理 WAL/SHM 状态的情况下直接复制正在使用的 SQLite 数据库。

常用只读检查：

```bash
actanara doctor
actanara doctor --scheduler
actanara update --dry-run
```

不要移动或复用已发布的版本 tag。如果已发布的制品或校验值被撤回，请停止分发，并在新版本发布后安装新版本。

## 更多操作

- [完整的中文本地操作 Runbook](local-operations-runbook.zh-CN.md)
- [CLI 产品边界（English）](cli-boundary.md)
- [nova-RAG 外部 Agent 合约（English）](rag-external-agent-contract.md)
- [安全策略](../SECURITY.md)
