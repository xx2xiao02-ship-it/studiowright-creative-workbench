# 音乐会话自动续期修复

适用 FlowMusic2API 基线：`34296d89e830e42b1b9582215404c7c5fae2ed27`。
补丁与对应测试随工作台保存，不向第三方上游仓库推送。

- Supabase 会话续期成功后，保存新的访问令牌、刷新令牌、Cookie 和有效期。
- 不再把 Google provider token 当作应用会话令牌，也不要求 Google OAuth client secret 才能延续已有会话。
- 续期失败后若尝试备用凭据，仍保留最初的会话错误，避免掩盖 `refresh_token_already_used`。
- 额度读取与会话续期是两件事；Gemini 额度读取失败不影响已验证可调用账号。

在外部服务的干净源码上运行：

```sh
python3 /path/to/workbench/packaging/music/apply_session_renewal.py /path/to/FlowMusic2API
python3 /path/to/workbench/packaging/music/apply_session_renewal.py /path/to/FlowMusic2API --apply
```

该工具校验基线及已有修改，并处理上游异常换行。应用后运行 `go test ./...`，构建新镜像。
部署必须暂停新派发并等待现有任务自然完成，保存旧镜像和源码以便回退；不要直接覆盖运行中的程序。

## 浏览器会话交接

已失效的刷新令牌不能通过改到期时间恢复，必须重新授权。核实邮箱后通过账号更新接口导入新会话。
导入成功后关闭该账号的空闲音乐页面，并仅清除 Flow Music 的会话 Cookie，让后台独占这套会话的续期。
不要调用退出登录接口（会撤销后台也在使用的会话），不要关闭 Flow 生成窗口。
浏览器与后台同时轮换同一套刷新令牌可能导致旧令牌被拒绝；后续导入工具必须遵循上述交接流程。

## 本次验证

- 工作台 87 项 Python 测试通过，包含辅助窗口过滤、额度异常不阻塞就绪。
- FlowMusic2API 全部 Go 包测试通过，覆盖会话续期、轮换落库、账号选择、代理和 HTTP 接口。
- 两个 Gemini 账号分别完成一次真实新调用，仅解除旧限制，保留旧请求记录。
- 部署期间等待后台任务结束后才重启；未重新提交历史任务。
- 线上仪表盘实测：Flow 9/10、Gemini 9/10、Music 9/9；页面无脚本错误。七个过期音乐账号重新授权后均完成续期及认证检查。

## 仪表盘音乐一键就绪

音乐圆圈点击后调用 `/api/music/readiness`，显示逐账号结果；刷新页面可重新读取仍在进行的检查。
只修复启用中、会话已过期的账号。已经就绪的账号不调用续期或认证接口；手动停用账号保持停用。
检测到音乐任务运行或任务状态不可读取时停止账号修改。每次修改前重新读取账号状态。
修复仅续期并验证服务认证，不创建音乐、不自动启用账号、不重复失败操作。连续点击复用当前检查。

## 音乐账号优先顺序

工作台按账号页顺序调用音乐服务。音乐后端用每个候选账号的实时额度判断是否可用。未出现在账号页的音乐账号按登记先后排在后面。生成前收到明确拒绝才会换号，已接收结果继续查原单。

在基线源码或已应用上述会话续期补丁的隔离副本中验证两项补丁：

```sh
python3 /path/to/workbench/packaging/music/apply_session_renewal.py /path/to/FlowMusic2API --priority-dispatch
python3 /path/to/workbench/packaging/music/apply_session_renewal.py /path/to/FlowMusic2API --priority-dispatch --apply
go test ./...
```

工作台在派发前探测 `/v1/audio/generations/priority`。识别到新版入口时发送账号顺序，旧服务继续走原生成接口。确认音乐镜像提供新版入口后再验收优先顺序。生成请求只提交一次，已经提交的任务按原记录查回结果。
