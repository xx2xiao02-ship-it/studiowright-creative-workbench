# StudioWright Windows 与 Ubuntu 部署手册

Windows 安装桌面操作端，通过 SSH 打开 Ubuntu 上的工作台。Ubuntu 负责运行 Flow、Gemini 和音乐任务，并保存账号、项目、素材和任务记录。已有 Ubuntu 工作台优先选择复用。新机器按本手册先装基础环境，再逐项接入服务。

## 1. 源码和运行位置

| 模块 | 仓库位置 | 作用 |
| --- | --- | --- |
| Windows 桌面端 | [`packaging/windows/client`](../packaging/windows/client) | WinForms 窗口、WebView2、系统托盘、SSH 连接和局域网发现 |
| Windows 账号助手 | [`packaging/windows/client-payload`](../packaging/windows/client-payload) | Chrome 扩展、账号管理、浏览器与本机助手通信 |
| Ubuntu 安装器 | [`packaging/ubuntu/server`](../packaging/ubuntu/server) | 安装依赖、生成服务配置、部署桌面助手和局域网发现 |
| Flow 后台 | [`packaging/ubuntu/snapshot/flow/console`](../packaging/ubuntu/snapshot/flow/console) | 账号、积分、项目、窗口、任务、素材库和调度 |
| Flow 执行器 | [`packaging/ubuntu/snapshot/flow/source`](../packaging/ubuntu/snapshot/flow/source) | `sw` 命令行程序及 Flow、Flow Music 浏览器驱动 |
| Ubuntu 工作台与统一 API | [`packaging/ubuntu/snapshot/workbench`](../packaging/ubuntu/snapshot/workbench) | 面板、账号治理、Gemini、音乐、素材分析和对外 API |
| GNOME 桌面组件 | [`desktop`](../packaging/ubuntu/snapshot/desktop) 和 [`workbench-helpers`](../packaging/ubuntu/snapshot/workbench-helpers) | 定位固定 Chrome 窗口和连接键鼠输入助手 |
| 音乐后台补丁 | [`packaging/music`](../packaging/music) | FlowMusic2API 的会话续期和账号优先级补丁，音乐服务本体单独部署 |
| 功能测试 | [`tests`](../tests) | Flow 窗口与素材、Gemini、音乐、API 和恢复逻辑 |

发布以 GitHub `main` 上的目标提交为准，在干净的 Git 工作副本中构建。开发机的 `unified-workbench/` 仍有 Windows 自动复用连接所需的启动入口。`coordinator/` 保存独立调度服务源码及运维记录，`release-workflow/` 和 `安装包/` 保存本地构建与验收材料。目录用途及整理顺序见 [代码地图](CODEBASE.md)，任务走向见 [工作拓扑](WORKFLOW.md)。

### 端口与网络

| 端口 | 所在机器及范围 | 用途 |
| --- | --- | --- |
| `22/TCP` 或自定义 SSH 端口 | Ubuntu，限授权连接来源 | Windows 登录与本机端口转发 |
| `18888/TCP` | Ubuntu 本机 | Flow 原后台 |
| `18920/TCP` | Ubuntu 本机 | 工作台页面与内部接口 |
| `18922/TCP` | Ubuntu 本机 | 带工作台密钥的统一 API |
| `18925/UDP` | Ubuntu 局域网 | Windows 查找服务器。禁用发现时可手动填地址 |
| `18890/TCP`、`18921/TCP` | 现有 Ubuntu 本机 | 可选的 Chrome 管理服务与独立调度服务，基础安装器未单独部署这两项 |
| `8000/TCP` | 音乐接入启用时供本机工作台访问 | 工作台源码默认连接的独立音乐后台端口 |

跨机访问面板和 API 使用 SSH 转发。账号管理接口、Chrome 调试端口和运行数据均保留在服务器的受控环境。

Windows 的 Chrome 管理助手也使用本机 `18890`。桌面客户端负责启动自己的助手，普通 SSH 转发只需要部署表中的 Flow、工作台和统一 API 三个端口。

## 2. Ubuntu 服务端

### 准备环境

1. 准备 Ubuntu 24.04 桌面版、x86-64、GNOME 46。用实际运行 Chrome 的普通桌面用户安装并启动工作台。准备 Python 3.12 和 Google Chrome。Chrome 必须能在该用户的桌面正常打开。
2. 从 [GitHub Releases](https://github.com/xx2xiao02-ship-it/studiowright-creative-workbench/releases) 下载匹配版本的 `.run` 安装包，或用第 6 节从指定提交打包。用 `sha256sum` 对照同一版本的校验值。
3. 为 Windows 准备 SSH 登录。Ubuntu 的 `system-deps` 模式会安装 `openssh-server`、Python 虚拟环境、GNOME Portal、Xvfb 和剪贴板工具。核实 SSH 主机指纹后再让 Windows 连接。参见 [Ubuntu OpenSSH 配置](https://documentation.ubuntu.com/server/how-to/security/openssh-server/)。

```bash
# 把下面的文件名换成本次下载或构建的实际包名
installer=./StudioWright-Ubuntu-24.04-Setup-0.3.7.run
sha256sum "$installer"
bash "$installer" --mode inspect
```

### 已有工作台

用运行服务的 Ubuntu 用户执行。`reuse` 核对现有工作台、Flow 和统一 API。需要 Windows 自动发现服务器时加 `--enable-discovery`。SSH 使用非默认端口时用 `--discovery-ssh-port` 填写实际端口。

```bash
bash "$installer" --mode reuse --enable-discovery
```

### 全新安装

先安装系统依赖，再以普通桌面用户安装。系统依赖步骤会提示使用 `sudo`。`new` 会检查现有文件、服务和端口，发现冲突就停止。需要接电常开时，可在第二条命令加 `--keep-awake-on-ac`。

```bash
bash "$installer" --mode system-deps
bash "$installer" --mode new
```

安装完成后退出 Ubuntu 桌面并重新登录。系统询问键鼠输入授权时，按桌面提示允许远程交互。随后在工作台核对 GNOME 窗口定位与任务输入状态。保留账号已有的输入方式。使用 HID 的主机由管理员根据设备和权限另行配置，干净安装按默认 GNOME 桌面流程验收。

| 路径或服务 | 用途 |
| --- | --- |
| `~/.local/share/studiowright-evaluation/` | Flow 源码和控制台，`venv/` 为服务 Python 环境，`console-data/`、`data/`、`output/` 和素材目录保存运行资料 |
| `~/.local/share/creative-unified-workbench/` | Ubuntu 工作台与统一 API 程序 |
| `~/.local/state/creative-unified-workbench/` | 账号状态、任务、授权、API 密钥库、窗口绑定和桌面助手运行文件 |
| `~/.config/systemd/user/` | 当前用户的服务及可选覆盖配置 |
| `studiowright-console.service` | Flow 后台 |
| `creative-unified-workbench.service` | 工作台面板 |
| `creative-unified-api.service` | 程序调用入口 |
| `studiowright-discovery.service` | 局域网发现 |
| `studiowright-flow-browser-recovery.service`、`studiowright-boot-readiness.service` | GNOME 登录后恢复浏览器并检查就绪状态 |

## 3. Windows 操作端

1. 准备 Windows x64 和 [Microsoft Edge WebView2 Runtime](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。使用安装包时，嵌入式 Python 随包提供。
2. 运行 `StudioWright-Windows-Desktop-Setup.exe`，选择「安装到当前用户」。程序默认放在 `%LOCALAPPDATA%\StudioWrightDesktop\app`，账号助手的本机桥接由安装器登记。
3. 打开「连接 Ubuntu 工作台」。通过局域网发现选择服务器，或手动填写 Ubuntu 地址、SSH 端口及用户名。首次连接先向管理员核对服务器指纹。密码仅供当前会话使用，已有受信任的 SSH 密钥时可留空。
4. 连接后打开工作台总览。页面和本机 API 分别使用 `http://127.0.0.1:18920/` 与 `http://127.0.0.1:18922`。窗口关闭或最小化后，可从托盘找回。升级客户端后，从托盘退出并重新启动一次。

### PowerShell 手动连接

桌面端已建立转发时直接复用，避免同一端口重复占用。需要独立排查时，在 PowerShell 先验证 SSH，再执行端口转发。把示例主机、用户、端口换成实际值。保留首次连接的主机指纹核对。

```powershell
$ubuntuHost = 'ubuntu-host.example'
$ubuntuUser = 'desktop-user'
$sshPort = 22
ssh -p $sshPort "$ubuntuUser@$ubuntuHost"

# 结束交互式 SSH 后，在单独的 PowerShell 窗口保持转发运行
ssh -N -T -o ExitOnForwardFailure=yes -p $sshPort -L 127.0.0.1:18888:127.0.0.1:18888 -L 127.0.0.1:18920:127.0.0.1:18920 -L 127.0.0.1:18922:127.0.0.1:18922 "$ubuntuUser@$ubuntuHost"
```

在另一个 PowerShell 窗口用以下命令验收。

```powershell
Invoke-RestMethod 'http://127.0.0.1:18920/api/health'
Invoke-RestMethod 'http://127.0.0.1:18922/health'
```

## 4. 接入账号、Gemini 与音乐

### Flow

在账号管理中登记账号，用该账号在 Ubuntu 独立的 Chrome 浏览器资料中完成 Google 登录。在原账号下打开或复用 `FLOW+邮箱前缀` 项目。检查实际登录身份和项目一致后，绑定该账号的固定任务页，刷新积分。总览的 Flow 就绪检查会核对固定页、输入框、积分和派发条件。

浏览器重开后如果提示「固定任务页找不到」，先检查原账号窗口是否停在登录或介绍页。在该窗口完成 Google 登录，回到已有项目后运行「Flow 一键就绪」。保留原项目和任务记录。窗口仍有草稿、运行任务或待核对结果时，先处理该状态。恢复逻辑见 [自动恢复说明](AUTO_RECOVERY.md)。

### Gemini

1. 在 Ubuntu 安装 [Google Antigravity 官方 CLI](https://antigravity.google/docs/cli/install/)。管理员核实实际 `agy` 文件的绝对路径。每个账号使用独立运行目录，目录包含 `home/` 和 `workspace/`。
2. 在状态目录维护 `official-runtime.json`，权限设为仅服务用户可读写。配置包含 `schema_version`、`enabled`、`binary`、`model`、`accounts`。`accounts` 以工作台登记的 Flow `profile` 的 SHA-256 值为键。配置值至少包含实际授权 `email` 和该账号独立的绝对 `root`。
3. 在每个账号的 `home/.gemini/antigravity-cli/settings.json` 保留运行时代码要求的工具权限限制。源码参见 [`official_runtime.py`](../packaging/ubuntu/snapshot/workbench/official_runtime.py)。账号高级模型只登记官方实际开放的型号。
4. 保持 Ubuntu 桌面登录，并先让该账号的 Flow 固定任务页就绪。在总览点击「Gemini 一键就绪」，由本人在 Ubuntu 上对应账号的 Chrome 窗口完成官方授权。复查云端回应和额度。流程见 [Gemini 一键就绪](Gemini-readiness.md)。

配置字段示意如下。`ACCOUNT_KEY_SHA256`、路径和邮箱都需要替换后才可使用。

```json
{
  "schema_version": 1,
  "enabled": true,
  "binary": "/home/desktop-user/bin/agy",
  "model": "gemini-3.8-flash-medium",
  "accounts": {
    "ACCOUNT_KEY_SHA256": {
      "email": "account@example.com",
      "root": "/home/desktop-user/.local/share/studiowright-gemini/account-a"
    }
  }
}
```

可以用 Ubuntu 的 `sha256sum` 计算登记项的 `profile`。`printf` 保留原始标识且不加换行。

```bash
printf '%s' '实际的 Flow profile 标识' | sha256sum
python3 -m json.tool "$HOME/.local/state/creative-unified-workbench/official-runtime.json" >/dev/null
```

新建账号时，先为这一个账号建立目录，再配置官方 CLI 的权限。已有设置时，保留其他键并合并下面的 `permissions.deny`。首次授权由工作台写入账号自己的登录资料。

```bash
account_root="$HOME/.local/share/studiowright-gemini/account-a"
install -d -m 700 "$account_root/home/.gemini/antigravity-cli" "$account_root/workspace"
```

```json
{
  "permissions": {
    "deny": ["command(*)", "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "mcp(*)"]
  }
}
```

Gemini 网络检查使用 Ubuntu GNOME 的 HTTPS 代理设置。出现「连不上 Google」时，先核对 Ubuntu 的代理软件、当前线路，以及 Google 登录和 Gemini 地址的连通性。截图中的「额度」来自官方 `/usage`，是否可派发看实时就绪状态。

### 音乐和统一 API

音乐需要单独部署 FlowMusic2API。基线、续期和优先派发补丁见 [音乐部署说明](../packaging/music/README.md)。在隔离源码上验证补丁并运行 `go test ./...`，再部署容器。配置本机端口和数据卷，逐个接入已授权的音乐账号。

工作台从私有文件读取 `FLOWMUSIC_ADMIN_USER`、`FLOWMUSIC_ADMIN_PASSWORD` 和 `FLOWMUSIC_DEFAULT_API_KEY`。现有代码带有主机专用的凭据及数据库路径。迁移到其他用户名或目录时，在部署前核对 [`server.py`](../packaging/ubuntu/snapshot/workbench/server.py) 中的路径与新主机布局。保留音乐会话、数据库和素材卷的独立备份。音乐优先派发需要新版后台提供 `/v1/audio/generations/priority`。

工作台的 API 密钥从面板的密钥管理生成。调用方在本机用环境变量 `STUDIOWRIGHT_API_KEY` 保存密钥，按 [统一 API 说明](API.md) 发请求。超时后用原请求编号查询，等待已有任务的最终结果。

## 5. 验收与故障定位

在 Ubuntu 以运行工作台的用户执行以下检查。三条 HTTP 检查预期返回 `200`，并各自给出对应应用名。

```bash
systemctl --user is-active studiowright-console.service creative-unified-workbench.service creative-unified-api.service
curl --noproxy '*' -fsS http://127.0.0.1:18888/health
curl --noproxy '*' -fsS http://127.0.0.1:18920/api/health
curl --noproxy '*' -fsS http://127.0.0.1:18922/health
curl --noproxy '*' -fsS http://127.0.0.1:18920/api/flow-readiness
journalctl --user -u creative-unified-workbench.service -u studiowright-console.service --since '15 minutes ago' --no-pager
```

再从 Windows 打开同一个工作台。核对账号列表、Flow 实际窗口检查、Gemini 账号状态、音乐状态及素材库能否读取。HTTP 健康说明服务已启动。业务验收按实际需要选用短对话、专用项目或测试音频，并单独记录结果。

| 现象 | 先查什么 |
| --- | --- |
| Windows 找不到服务器 | SSH 登录能否成功、Ubuntu 发现服务状态、局域网 UDP 18925 是否可达 |
| Windows 提示端口占用 | 本机是否已有桌面客户端或 SSH 转发。检查进程归属后复用原连接 |
| Flow 显示「同步暂缓 · 上次结果」 | 看 Flow 后台 `/api/state`、工作台 `/api/flow-readiness` 和日志。面板暂时显示最近一次检查结果 |
| 固定项目找不到 | 查原账号浏览器是否重新启动、原项目是否仍存在、该窗口是否需要登录，并重新核对绑定 |
| Gemini 没弹授权页 | 先看工作台的 Ubuntu 到 Google 连通检查。代理端口能接收请求后，还要核对 Google 地址实际能否通过该线路访问 |
| 音乐帐号读不到 | 核对容器、私有凭据文件、数据路径和优先派发端点，同时检查是否有正在运行的音乐任务 |

## 6. 从源码构建安装包

在独立副本中检出拟发布的提交。记录 `git rev-parse HEAD` 和 `VERSION`。构建产物放在 `dist/`，核对版本并计算 SHA-256。仓库源码、构建依赖和运行数据分别保管。

### Ubuntu 安装包

在 Ubuntu 24.04 x86-64、Python 3.12 环境准备离线 wheel 目录。可使用现有已校验的 wheel 集合，也可从受信任的软件源按锁定清单下载。构建脚本会验证清单中的快照文件和指纹，并打包已校验快照与离线依赖。修改快照源码时，先检查 Git 变更并将新增快照文件纳入 Git，再用 `--refresh-snapshot` 更新清单，随对应源码一起提交。打包后在隔离的 Ubuntu 24.04 上执行第 2 节和第 5 节。部署机已有生产数据时，先验证 `inspect` 或 `reuse`。

```bash
python3.12 -m venv .venv-build
.venv-build/bin/python -m pip download --only-binary=:all: \
  --dest packaging/ubuntu/wheelhouse-linux/flow \
  -r packaging/ubuntu/snapshot/flow/requirements-frozen.txt
.venv-build/bin/python -B packaging/ubuntu/build_server.py --check-snapshot
.venv-build/bin/python -B packaging/ubuntu/build_server.py
sha256sum packaging/ubuntu/dist/StudioWright-Ubuntu-24.04-Setup-*.run
```

### Windows 安装包

1. 在 Windows x64 准备构建用 Python 和 `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`。把 x64 [Python 嵌入式发行包](https://docs.python.org/3.13/using/windows.html#the-embeddable-package) 解压到 `packaging/windows/client-payload/runtime/`，确认其中有 `python.exe`。本机已有构建材料中使用 3.13.17。
2. 执行 `fetch_webview.py` 准备脚本固定版本的 WebView2 SDK。Windows 构建脚本会向 NuGet 下载锁定的 SSH.NET 及依赖并校验包指纹。若解析到代理 Fake-IP 或其他非公网地址，脚本会停止。构建机应使用能正常解析并访问 NuGet 的网络。
3. 在没有旧构建产物的副本中运行下列命令。现有构建产物先另行留存。构建完成后在测试用户或测试机进行安装和 SSH 连接验收。

```powershell
python -B packaging/windows/fetch_webview.py
python -B packaging/windows/build_client.py
Get-FileHash -Algorithm SHA256 'packaging/windows/dist/StudioWright-Windows-Desktop-Setup.exe'
```

项目 Python 功能测试优先在隔离的 Ubuntu 24.04、Python 3.12 环境运行。部分服务端测试会使用 Linux 独有的目录及行为。在测试环境安装相应测试依赖后，从仓库根目录执行 `python -m pytest -q`。

## 7. 现有主机、升级和回退

2026-11-24 核对 Windows 本机为 x64，已安装 StudioWright 桌面端和账号助手。本机的 `18888`、`18920`、`18922` 健康接口均可访问。开发机的 PowerShell、Git、Python 和 .NET 编译器均已就位，Windows 构建仍需有效的 NuGet 解析和连接。

2026-11-24 通过 SSH 核对了现有 Ubuntu。主机运行 Ubuntu 24.04 x86-64、GNOME 46、Python 3.12。三个基础 HTTP 健康接口均返回 `200`，独立局域网发现服务已监听。另有独立调度服务、调度状态备份定时器、Chrome 管理和音乐容器。调度程序位于 `~/.local/share/creative-unified-coordinator/`。当前 GNOME 本机代理端口为 `7897`。运维时以目标机器的实际配置为准。

对照本次清理前的远程基线 `ebd7c5b`，按统一换行后的文件指纹核对运行文件。

| 目录 | 对照结果 |
| --- | --- |
| 工作台 | 54 个文件一致，9 个内容不同，1 个仓库文件在运行目录没有同名文件。涉及面板、派发、账号治理和固定页检查 |
| Flow 后台 | 30 个文件一致，9 个内容不同，2 个仓库文件未在运行目录找到。涉及积分、浏览器、账号检查及任务窗口维护 |
| Flow 执行器 | 22 个文件一致，2 个内容不同，差异位于 Chrome 管理和 Flow 页面驱动 |
| GNOME 扩展、桌面助手 | 抽核的 4 个文件全部一致 |

运行目录还有历史运维工具。独立调度服务及备份定时器、主机代理、账号授权、音乐部署参数和已有素材不由基础安装器一起恢复。线上升级前先核对待更新模块，保留已部署文件、服务配置和状态备份。运行中任务自然完成后再切换。优先在隔离环境通过安装和业务验收，再安排线上更新。

备份时记录目标提交、安装包指纹、受影响服务、各运行目录以及外部素材卷位置。SQLite 使用一致性备份，或在服务安全停机后备份相关数据。SSH 私钥、Google 授权与 API 密钥存放在受控的主机或加密备份中。回退使用同一版本的程序、配置和兼容状态备份，再重做第 5 节的检查。
