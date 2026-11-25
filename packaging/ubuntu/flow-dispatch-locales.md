# Flow 账号派发自检

在 Flow 安装目录的 `console-data/dispatch-locales.json` 中按账号的本地浏览器配置名称设置语言和时区。默认安装目录为 `~/.local/share/studiowright-evaluation`。以下为示例，请使用账号实际批准的设置。

```json
{
  "account-example": {
    "languages": ["zh-CN", "zh"],
    "timezone": "Asia/Shanghai"
  }
}
```

语言按 Chrome 设置中的顺序核对，时区使用 Ubuntu 当前配置的 IANA 时区名称。缺少配置、浏览器语言或系统时区不匹配，以及扩展或代理自检失败时，账号的图片和视频任务会留在队列。

后台仅对 `preflight.py` 中逐个核对并启用的账号执行离线派发自检。已启用账号允许 Gemini in Chrome、Google Network Speech、Chrome PDF Viewer、Google Hangouts 和 Chrome Web Store Payments。每项都核对组件 ID、清单公钥和安装来源。其他扩展或来源不符的组件仍会拦截。其他账号沿用已有派发规则，页面告警停派和可选在线自检保持原有行为。

`account-f8d81e309793` 保留 Chrome 默认安装的 Google Docs Offline，`ub-chrome-f8f5d94158` 保留 Chrome 语音输入连接器。这两项分别限定到已核对的账号，并核对组件 ID、安装位置、磁盘清单和 Chrome 校验文件。

后台在核对登录或检查已连接的 Flow 页面时遇到异常活动提示，会先尝试保存截图，再停派该账号。在工作台账号卡片上核查后可手动解除这条停派记录。其他派发限制仍会逐项核对。

## Windows 同步新增账号的代理

在 Flow 安装目录的 `bin/exit-inventory.json` 中记录已核实的出口。键为 Ubuntu 本机 SOCKS 端口，值包含 `public_ip` 和布尔字段 `confirmed`。`public_ip` 填上游实际使用的公网 IPv4 或 IPv6 地址。库存文件由运行 Flow 的用户所有，禁止其他用户写入。库存须覆盖 `bin/exit-map.txt` 中已有绑定的已确认出口，才能核对新出口是否与它们共享公网 IP。

Windows 同步生成的 `account-*` 配置沿用已有绑定。首次需要分配时，后台沿用 `exit-map.txt.lock` 加锁，从已确认的独立空闲出口中认领一个，再进行正常登录核对。库存缺失、无合适出口或正在编辑映射时，原同步任务留在队列。Flow 账号卡片显示已绑定的端口和已核实的公网 IP。移除 Flow 账号后，后台在原浏览器退出且账号操作结束后解除绑定，随后可把该出口分配给新账号。登录包仍按 30 分钟有效期处理，过期后需要在 Windows 重新同步。维护库存时使用同一把锁，保持同一公网出口的地址记录一致。后台通过库存分配已准备的出口，公网 IP 的申请和路由配置由运维处理。

## 可选在线自检

`preflight.dispatch_check(root, profile, online=True)` 先通过离线检查，再读取已运行的本机 Chrome。在线检查从已有 Flow 标签页或空白标签页读取 `navigator.webdriver`、UA、屏幕参数、Canvas 和 WebGL。`navigator.webdriver` 必须为 `false`，四项信号的组合摘要必须与基线一致。运行中的浏览器不可访问、信号不全或基线缺失时，在线自检失败。现有派发入口使用默认离线模式。

基线保存为 `console-data/fingerprint-baseline.json`，键为本地浏览器配置名称，值为 64 位小写 SHA-256 摘要。核查可信会话后，可在 Ubuntu 的 Flow Python 环境中调用 `preflight.collect_browser_fingerprint(root, profile)`。返回值为 `(摘要, 错误代码)`，只有错误代码为空时才可人工登记摘要。检查过程不会自动登记或更新基线。异步调用方可通过 `asyncio.to_thread` 调用这些同步函数。
