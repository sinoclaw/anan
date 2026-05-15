# Anan Agent — 安装指南

本文档涵盖所有支持的安装方式。**最快路径**请直接看 [README.zh-CN.md](README.zh-CN.md) 里的一键脚本。本指南面向需要更多控制的用户：选择 pip / Docker / 源码、在特定操作系统上运行、或部署为长期运行的服务。

## 目录

- [选择安装方式](#选择安装方式)
- [方式 1 — 一键安装脚本（推荐）](#方式-1--一键安装脚本推荐)
- [方式 2 — pip / pipx（PyPI）](#方式-2--pip--pipxpypi)
- [方式 3 — Docker / docker-compose](#方式-3--docker--docker-compose)
- [方式 4 — 源码安装（开发者）](#方式-4--源码安装开发者)
- [操作系统说明](#操作系统说明)
  - [Linux（Ubuntu/Debian、RHEL/CentOS、Arch）](#linux)
  - [macOS（Intel 与 Apple Silicon）](#macos)
  - [Windows（原生 + WSL2）](#windows)
  - [Android / Termux](#android--termux)
- [部署为系统服务（systemd）](#部署为系统服务systemd)
- [升级](#升级)
- [卸载](#卸载)
- [故障排查](#故障排查)

---

## 选择安装方式

| 方式 | 适合人群 | 优点 | 缺点 |
|------|---------|------|------|
| **一键脚本**（`install.sh` / `install.ps1`）| 大多数用户 | 自动处理 uv、Python 3.11、Node.js、ripgrep、ffmpeg | 会修改 shell 配置文件 |
| **pip / pipx** | 已有 Python 3.11+ 环境的用户 | 熟悉、按用户隔离 | 需要自己装 Node.js / ripgrep / ffmpeg |
| **Docker** | 服务器、隔离部署、CI | 完全可复现、不污染宿主 | 镜像约 2 GB，需要 Docker daemon |
| **源码安装** | 贡献者、插件开发者 | 可编辑安装、含全部开发工具 | 步骤更多 |

经验法则：
- **个人笔记本** → 一键脚本
- **VPS / 家庭服务器** → Docker（或 一键脚本 + systemd）
- **已有 Python 项目** → `pip install anan`
- **二次开发 Anan 本身** → 源码安装

---

## 方式 1 — 一键安装脚本（推荐）

### Linux / macOS / WSL2 / Termux

```bash
curl -fsSL https://raw.githubusercontent.com/anan/anan/main/scripts/install.sh | bash
```

执行内容：
1. 安装 [`uv`](https://docs.astral.sh/uv/)（高性能 Python 包管理器）
2. 安装 Python 3.11（隔离的，不影响系统 Python）
3. 通过系统包管理器安装 Node.js、ripgrep、ffmpeg
4. 创建 `~/.anan/` 数据目录
5. 创建 `~/.local/bin/anan` 软链接
6. 必要时把 `~/.local/bin` 加入 `PATH`

安装完成后：
```bash
source ~/.bashrc        # 或 ~/.zshrc
anan                # 开始对话
```

### Windows（PowerShell，原生）

```powershell
irm https://raw.githubusercontent.com/anan/anan/main/scripts/install.ps1 | iex
```

Windows 安装程序额外打包 **MinGit**（约 45 MB 便携版 Git Bash，无需管理员权限），解压到 `%LOCALAPPDATA%\anan\git`。Anan 用这个隔离的 bash 执行 shell 命令，不依赖、不影响系统已有的 Git。

> 原生 Windows 是**早期 Beta**。最稳定的 Windows 路径是在 **WSL2** 里跑 Linux 一键脚本。

---

## 方式 2 — pip / pipx（PyPI）

PyPI 包名为 **`anan`**。

### pipx（CLI 工具推荐方式）

```bash
# 没有 pipx 先装一下
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# 安装 Anan（含全部可选功能）
pipx install "anan[all]" --python python3.11
```

### pip（在 venv 里）

```bash
python3.11 -m venv ~/sinoclaw-venv
source ~/sinoclaw-venv/bin/activate
pip install "anan[all]"
anan --help
```

### 可用 extras

| Extra | 包含内容 |
|-------|---------|
| `[all]` | 推荐 — 完整功能集（语音、浏览器工具、图像生成等） |
| `[dev]` | 开发工具（pytest、ruff、ty）— 贡献者用 |
| `[termux]` | Android/Termux 精选子集（跳过在 Android 上无法编译的语音依赖） |
| `[rl]` | RL/Atropos 训练环境（重量级：torch + tinker + wandb） |

> **你仍然需要单独安装 Node.js、ripgrep、ffmpeg**，否则消息网关、代码审查、语音工具无法完整工作。请用系统包管理器装（见下方各操作系统说明）。

### 验证安装

```bash
anan --version
anan doctor          # 诊断缺失的系统依赖
```

---

## 方式 3 — Docker / docker-compose

Docker 是在 **VPS 或家庭服务器**上运行 Anan 而不污染宿主系统的最简单方式。

### docker-compose 快速启动

```bash
git clone https://github.com/anan/anan.git
cd anan
SINOCLAW_UID=$(id -u) SINOCLAW_GID=$(id -g) docker compose up -d --build
```

会启动两个服务：
- **gateway** — 消息网关（Telegram、Discord、Slack 等），使用宿主网络
- **dashboard** — 浏览器仪表盘，监听 `127.0.0.1:9119`（默认仅 localhost，安全考虑）

配置数据保存在宿主 `~/.anan/`，挂载到容器内 `/opt/data`。

### 首次配置

```bash
# 进入运行中的容器
docker exec -it -u anan $(docker ps -qf name=gateway) bash

# 运行配置向导
anan setup
```

或者直接在 `docker-compose.yml` 里通过环境变量配置凭据（文件里有 Telegram、Discord、Teams 等的注释示例）。

### 手动构建镜像

```bash
docker build -t anan:latest .
docker run -d \
  --name anan \
  --network host \
  -v ~/.anan:/opt/data \
  -e SINOCLAW_UID=$(id -u) \
  -e SINOCLAW_GID=$(id -g) \
  anan:latest gateway run
```

### 常用命令

```bash
docker compose logs -f gateway        # 实时查看网关日志
docker compose restart gateway        # 改完配置后重启
docker compose down                   # 停止全部服务
docker compose pull && docker compose up -d --build   # 升级
```

### 安全说明

- Dashboard 仅绑定 `127.0.0.1` — 它存储 API 密钥，没有认证就暴露到 LAN 是不安全的。远程访问请用 SSH 隧道：`ssh -L 9119:localhost:9119 your-server`。
- OpenAI 兼容 API server 默认**关闭**。要启用，请在 `docker-compose.yml` 里取消注释 `API_SERVER_HOST` 和 `API_SERVER_KEY` —— **key 是必填**。
- `SINOCLAW_UID` / `SINOCLAW_GID` 应该与拥有 `~/.anan` 的宿主用户一致，否则文件在宿主端无法读写。

---

## 方式 4 — 源码安装（开发者）

适合贡献者和插件开发者，编辑代码立即生效。

```bash
git clone https://github.com/anan/anan.git
cd anan

# 简单路径 — 用自带的引导脚本
./setup-anan.sh

# 直接从 checkout 运行
./anan
```

`setup-anan.sh` 会自动：安装 uv、用 Python 3.11 创建 `.venv`、可编辑安装 `.[all]`、把 `~/.local/bin/anan` 软链到 checkout。

手动等价命令：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh        # 跑测试
```

> **国内用户镜像加速：** GitHub clone 慢可以走 GitCode 镜像：
> ```bash
> git clone --recurse-submodules https://gitcode.com/GitHub_Trending/si/anan.git
> ```
> 或者 Gitee：`git clone https://gitee.com/anan/anan.git`

---

## 操作系统说明

### Linux

如果你走 pip 路径，需要先装好系统依赖：

**Ubuntu / Debian：**
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip nodejs npm ripgrep ffmpeg git
```

**RHEL / CentOS / Fedora：**
```bash
sudo dnf install -y python3.11 nodejs npm ripgrep ffmpeg git
```

**Arch / Manjaro：**
```bash
sudo pacman -S python nodejs npm ripgrep ffmpeg git
```

**Alpine（musl）：** 一键脚本目前不支持 Alpine，请改用 Docker 镜像。

### macOS

一键脚本依赖 **Homebrew** 安装系统依赖。如果还没有 Homebrew：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

然后跑一键脚本即可。Apple Silicon（M1/M2/M3）完全支持 — uv 会自动安装原生 arm64 Python。

走 pip 路径：
```bash
brew install python@3.11 node ripgrep ffmpeg git
pipx install "anan[all]" --python python3.11
```

### Windows

**推荐：** WSL2 + Linux 一键脚本。WSL2 安装：
```powershell
wsl --install -d Ubuntu-22.04
```
然后在 WSL 里：`curl -fsSL https://raw.githubusercontent.com/anan/anan/main/scripts/install.sh | bash`

**原生 Windows（早期 Beta）：** 用 README 里的 PowerShell 一键命令。Anan 安装在 `%LOCALAPPDATA%\anan`。

原生 Windows 已知限制（需要这些功能请用 WSL2）：
- 浏览器仪表盘聊天面板（需要 POSIX PTY）
- 部分终端后端（Singularity、某些云沙箱模式）

### Android / Termux

```bash
pkg update && pkg upgrade
pkg install python rust nodejs ripgrep ffmpeg git
curl -fsSL https://raw.githubusercontent.com/anan/anan/main/scripts/install.sh | bash
```

安装脚本会自动检测 Termux 并安装 `[termux]` extra（跳过在 Android 上无法编译的语音依赖）。完整指南：[Termux 快速开始](https://anan.nousresearch.com/docs/getting-started/termux)。

---

## 部署为系统服务（systemd）

把消息网关作为长期后台服务跑在 Linux 上：

```bash
sudo tee /etc/systemd/system/anan-gateway.service > /dev/null <<'EOF'
[Unit]
Description=Anan Agent — Messaging Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
Environment=HOME=/home/YOUR_USER
ExecStart=/home/YOUR_USER/.local/bin/anan gateway run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now anan-gateway
sudo systemctl status anan-gateway
journalctl -u anan-gateway -f       # 实时查看日志
```

把 `YOUR_USER` 替换成你的实际用户名。

> `install.sh` 检测到已配置消息平台 token 时会主动提示要不要装这个 service。

---

## 升级

| 安装方式 | 升级命令 |
|----------|---------|
| 一键脚本 | `anan update` |
| pipx | `pipx upgrade anan` |
| pip | `pip install -U "anan[all]"` |
| Docker | `docker compose pull && docker compose up -d --build` |
| 源码 | `git pull && uv pip install -e ".[all,dev]"` |

---

## 卸载

```bash
# 删除二进制软链和 venv
rm -rf ~/.local/bin/anan ~/.local/share/anan

# 删除用户数据（配置、会话、技能）— 不可恢复
rm -rf ~/.anan

# pip / pipx
pipx uninstall anan
# 或: pip uninstall anan

# Docker
docker compose down -v
docker rmi anan

# systemd
sudo systemctl disable --now anan-gateway
sudo rm /etc/systemd/system/anan-gateway.service
```

---

## 故障排查

### 安装后 `anan: command not found`
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### `anan doctor` 报告依赖缺失
用系统包管理器装上（见上方[操作系统说明](#操作系统说明)）。

### Python 版本错误（`requires Python >=3.11`）
```bash
# 用 uv 装
uv python install 3.11

# 或者用系统包管理器 — 见上方操作系统说明
```

### Docker：`~/.anan` 权限被拒绝
确保 `SINOCLAW_UID` 和 `SINOCLAW_GID` 与宿主用户一致：
```bash
SINOCLAW_UID=$(id -u) SINOCLAW_GID=$(id -g) docker compose up -d
```

### 国内网络：GitHub clone / pip install 太慢

```bash
# Git：用 GitCode 镜像
git clone https://gitcode.com/GitHub_Trending/si/anan.git

# pip：用清华源
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "anan[all]"

# npm：用 npmmirror
npm config set registry https://registry.npmmirror.com
```

### 还是不行？

- 跑 `anan doctor` — 诊断 30+ 种常见问题
- 查看日志：`anan logs --follow`
- 提 issue：https://github.com/anan/anan/issues
- Discord：https://github.com/anan/anan

---

📖 **完整文档：** https://anan.nousresearch.com/docs/
