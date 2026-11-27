# 创作工作台 API

工作台按请求类型分流：对话、推理、识图走 Gemini；生图、生视频走 Flow；音乐生成走音乐服务。不会根据正文改派通道。以上功能使用同一把工作台密钥，无需 Google API 密钥。统一 API 当前按 v1 兼容，新增字段会保持已有字段可用。

## 请求准备

调用地址是 `http://127.0.0.1:18922`。远程连接时需保持工作台的端口转发。请求带 `Authorization: Bearer <工作台密钥>`；JSON 用 `application/json`，文件原始字节用 `application/octet-stream`。

提交对话、识图或 Flow 生成前，用 `Idempotency-Key` 传入并保存一个 UUID。音乐生成的 UUID 放在正文 `request_id` 中。超时或返回 `unknown` 时用原编号查询，不能换新编号重发。

**健康检查与能力快照**

- `GET /health`：统一 API 存活检查，不需要密钥；它只说明 18922 端口可访问。
- `GET /api/health`：带密钥读取 API v1、部署信息和请求策略。
- `GET /api/snapshot`：读取当前 `flow_models`、`tasks`、`real_accounts` 以及可用状态。模型列表和账号状态会变化，提交前应重新读取。

健康检查和快照都不提交生成任务。

**常见 HTTP 状态**

| 状态 | 含义 | 处理方式 |
| --- | --- | --- |
| `200` | 查询成功或已复用同一请求记录 | 读取返回 JSON |
| `201` | 新请求记录已创建 | 保存返回的请求编号 |
| `202` | 已接收并排队 | 按原编号轮询，不重复提交 |
| `400` | 参数、正文或文件格式不正确 | 修正请求后使用新编号 |
| `401` | 密钥无效或已撤销 | 更新密钥后重试 |
| `403` | 来源、主机或权限不符合要求 | 保持本机/端口转发并检查权限 |
| `404` | 路径、任务或素材不存在 | 核对原编号和路径 |
| `409` | 请求状态不允许继续操作 | 先查询原任务或请求记录 |
| `502` / `503` | 上游或连接暂时不可用 | 保留原编号，稍后查询 |

错误响应统一为 `{"error":"原因"}`。收到连接超时、`502`、`503` 或 `unknown` 时，不能根据结果不明重发生成。

提交前读取账号池如果遇到临时连接故障、超时或后端暂时不可用，StudioWright 会自动重新读取一次。两次都失败时会返回「现有账号池暂不可读取」，这次提交不会入队。这个读取与模型重试独立，`retry_policy: never` 仍会禁止模型重试。

**Python 公共准备 · 先设置 STUDIOWRIGHT_API_KEY**

```python
import json, os
from pathlib import Path
from uuid import uuid4
from urllib.request import Request, build_opener, ProxyHandler

BASE = "http://127.0.0.1:18922"
AUTH = {"Authorization": "Bearer " + os.environ["STUDIOWRIGHT_API_KEY"]}
HTTP = build_opener(ProxyHandler({}))

def call(path, payload=None, request_id=None, *, file=None):
    headers = dict(AUTH)
    body = None
    if request_id:
        headers["Idempotency-Key"] = request_id
    if file is not None:
        headers["Content-Type"] = "application/octet-stream"
        body = Path(file).read_bytes()
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    with HTTP.open(Request(BASE + path, data=body, headers=headers), timeout=180) as res:
        return json.load(res)
```

## Gemini · 对话与推理

新文字对话和识图默认使用 `gemini-4-hydrogen-medium`。文字请求可通过 `model` 指定 HY、Neon 或 Carbon。实际可用档位取决于模型和账号。显式指定完整型号时，`effort` 须与型号一致。续聊省略 `model` 时沿用已保存的模型和账号，可调整同系列的思考等级。

第一次发送时只需提供对话编号、`account: "auto"` 和消息。
```python
chat = call("/api/conversations", {"title": "Hydrogen 对话示例"})
rid = str(uuid4())
submission = call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto",
    "prompt": "请分析这个方案的优缺点。"
}, rid)
print(submission["request_id"], submission["model"], submission.get("fallback"))
# 轮询 call(f"/api/real-pool/requests/{rid}") 查询最终型号和回复。
```
识图沿用图片上传回执。识图接口和只校验接口都接受 `effort` 与 `fallback_model`。


容量不足的受控重试：文字和识图业务仅在收到明确的容量不足 503 结束回执、且没有任何回复时，使用原账号和原请求编号，先等待 10 秒、再等待 30 秒，最多新增两次尝试。部分回复、结果未知、普通网络错误不触发此策略；`retry_policy: never` 禁止后台重试。每次重试前复查账号暂停、维护和请求状态；不换账号，不重放历史任务。验活和自动素材分析沿用原有规则。

若 CLI 曾报告容量不足 503，但后续在同一原生对话中给出明确的完整回复结束标记，且流式文字与最终全文逐字一致，后台确认 `completed`，并在说明及原始调用回执保留“早期容量503已恢复”的记录。仅有文字、没有完整结束证据，或出现后续错误、对话不符、进程中止时，不据此确认成功。历史记录的核对恢复不会创建新请求或增加尝试次数。

Gemini CLI 账号池默认总并发 50，可在操作台 Gemini 卡下设置 1～50。文字、识图与素材分析共用该名额，超出时排队。调低上限时，正在运行的任务继续执行。同一对话仍逐轮执行。工作台通过 `GET /api/gemini/concurrency` 读取设置与占用，使用 `POST /api/gemini/concurrency` 保存 `{"maximum": 50}`。供应商限流仍按实际返回处理。

文字和识图的单次模型等待默认最多 600 秒；连续 240 秒没有收到文字进展时停止等待，保留已收到的部分回复和未确认状态。排队时间另计；提交接口仍返回请求编号供轮询，不应将 HTTP 查询期限当成生成期限。

管理员可在工作台状态目录的 `gemini-execution.json` 配置 `text_total_seconds`、`image_total_seconds`、`idle_seconds`、`probe_total_seconds`、`probe_idle_seconds`，数值须为 1～3600 秒。业务默认 600/600/240 秒；验活默认总计 180 秒、无进展 120 秒。配置仅对下一次调用生效。

需要关闭某条 Gemini 文字或识图请求的自动重试时，可在请求体传 `"retry_policy": "never"`。两条通道均不再预留或发起额外尝试；该字段省略时沿用原有策略。同一请求编号重查时保持参数一致。

**`POST /api/conversations`** · 传 title，返回对话 id。

**`POST /api/real-pool/chat`** · 传 conversation_id、account: "auto"、prompt，返回 request_id 和 status。

### 模型与 Flash 保底

| 模型 | 可选 `effort` | 用途 |
| --- | --- | --- |
| `gemini-4-hydrogen` | `low`、`medium`、`high`、`xhigh` | 新请求主模型，参数全省略时使用 `medium` |
| `gemini-4-neon` | `low`、`medium`、`high`、`xhigh` | 可显式选择的文字模型 |
| `gemini-4-carbon` | `low`、`medium`、`high` | 可显式选择的文字模型，省略档位时用 `medium` |
| `gemini-3.8-flash` | `low`、`medium`、`high` | 首轮保底及已绑定 Flash 的历史对话 |

使用 `account: "auto"` 时，按账号池顺序选择开通对应型号、授权就绪且额度可用的账号。旧客户端首轮显式传 Flash 型号时，工作台会先尝试同档 HY，再使用 Flash 保底。已绑定的对话沿用实际模型和账号。

- 自动选账号的 HY 首轮在发送前没有可用主模型账号时，才使用 `gemini-3.8-flash-medium` 保底。`fallback_model: false` 可关闭保底，也可传受支持的 Flash 型号调整保底档位。显式选 Neon 或 Carbon 时需传受支持的 Flash 型号才启用保底。
- 入队前两个池都不可用时返回 HTTP 409。排队期间账号情况变化时，仍可以在第一次发送前更换账号或启用已设定的保底。已经开始调用、收到部分回复或结果待确认时，保留原模型、原账号和请求编号。
- 查询回执里的 `requested_model` 查看主模型，`model` 查看实际模型，`fallback.used` 和 `fallback.reason` 查看是否保底及原因。续聊使用原对话编号，新消息采用新请求编号。

```python
chat = call("/api/conversations", {"title": "只用 HY"})
rid = str(uuid4())
print("请求编号:", rid, flush=True)
submission = call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto",
    "prompt": "请列出这个方案的三个关键风险。",
    "fallback_model": False
}, rid)
print(submission["model"])
# 后续用 call(f"/api/real-pool/requests/{rid}") 查询最终型号和回复。
```

Google Antigravity 公告将于 2026 年 11 月 30 日移除 Gemini 3.8 Flash。Flash 保底受供应商实际可用性约束。[Google 模型说明](https://antigravity.google/docs/models?app=antigravity#reasoning-model)

**账号与额度监控**可在工作台「操作台 → Gemini 共享额度」查看。总览覆盖已登记的 Gemini 账号。5 小时和每周额度分别统计最近 10 分钟内读到的账号平均剩余比例，并显示各自的已读账号数。工作台本机面板端口 `18920` 的 `GET /api/channel-status` 返回默认文字模型 `dispatch.model` 与 `dispatch.advanced_pool`。`GET /api/official-quota` 返回各账号的 5 小时和每周剩余与恢复时间。`advanced_pool.quota_scope` 为 `gemini_models_shared`，表示这些数值来自各账号的官方 Gemini Models 共享额度。`advanced_pool.windows.*.total_remaining_percent` 是可读账号剩余百分比的合计，`average_remaining_percent` 是账号平均剩余。两个监控接口由面板提供，程序调用端口 `18922` 提供对话提交及结果查询。可读的高级账号每 5 分钟刷新。

接着问时沿用 `conversation_id`，为新消息生成新的请求编号。原账号受限时续聊暂缓。自动分配的新请求仅在确认还未发送时允许切换账号。查回复用下方「结果与下载」中的 Gemini 查询接口。

确认未发送时，临时启动故障可在原账号和原请求编号下重试一次。识图或视频抽帧在建立 Gemini 会话时超时（`protocol_timeout_2`），且图片尚未发送，也适用这条规则。`retry_policy: "never"` 会关闭这次重试。收到部分回复或结果不明时，按原请求编号查询。`attempts` 记录每次尝试的状态、原因和耗时。

**复制文字请求 · 实际发送**

```python
chat = call("/api/conversations", {"title": "我的对话"})
rid = str(uuid4())
print("请求编号:", rid, flush=True)
print(call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto", "prompt": "请用一句话介绍你自己。"
}, rid))
```

## Gemini · 模型使用推荐

固定格式的活先用低档，需要理解多份材料用 Gemini 4 Hydrogen 中档，遇到导演取舍或高返工成本的判断用 Gemini 4 Neon，疑难事项交给 Gemini 4 Carbon 深入复核。

### 按任务快速选

| 任务特征 | 先选 | 下一步 |
| --- | --- | --- |
| 结果好抽查，输入已经齐全 | Gemini 3.8 Flash 低档或 Gemini 4 Hydrogen 低档 | CLI 上把 Gemini 3.8 Flash 作为过渡。长期用 Gemini 4 Hydrogen 低档，账号只开中档时用 Gemini 4 Hydrogen 中档。另接 Google API 做大批量时，可小样验证 3.5 Flash-Lite 的 `minimal` 档。 |
| 要综合多篇资料、图片细节或已有分镜 | Gemini 4 Hydrogen 中档 | 输入完整但仍漏关系、因果或约束时，用 Gemini 4 Hydrogen 高档复核。 |
| 要定方案，错了会重拍、重生成或影响发布 | Gemini 4 Neon 中档或高档 | 普通分镜、方案比较用中档。复杂场景调度、重要结论与终审用高档。 |
| 多套方案都有漏洞，根因存疑，或要复核长材料 | Gemini 4 Carbon 中档或高档 | 明确核验项用中档。要追证据、找反例或检查多个边界条件时用高档。 |

### Gemini 3.8 Flash · 哪些活可以交给它

| 适合的场景 | 思考档位 | 实际怎么用 |
| --- | --- | --- |
| 搜新闻和公告、标题去重、短摘要 | 低档 `low` | 先用实时搜索取回来源。让 Gemini 3.8 Flash 汇总重点并保留标题、发布时间和原文链接。 |
| 固定字段提取、短文翻译、改标题、素材粗标签 | 低档 `low` | 有明确的输入和输出格式时用低档。图片或已抽好的视频帧可做主体、场景、风格初筛。 |
| 几条报道合成简报、按多条格式要求改稿、对照几张截图 | 中档 `medium` | 当低档漏字段或漏掉素材差异时升中档。让它给出对应原文或图号，方便复核。 |
| 沿用原 Gemini 3.8 Flash 对话做较复杂整理、对照旧工作流输出 | 高档 `high` | 适合保持旧项目的口径，或与 Gemini 4 Hydrogen 做同题对测。要查多来源矛盾、做长材料分析时，按 Gemini 4 Hydrogen、Gemini 4 Neon 或 Gemini 4 Carbon 的档位选用。 |

目前可在官方 CLI 仍列出 Gemini 3.8 Flash 时直接选用。算力中心 API 新对话提交旧 Gemini 3.8 Flash 型号会先尝试同档 Gemini 4 Hydrogen，Gemini 3.8 Flash 可用时可做发送前保底。按 Google 公告，Antigravity 计划于 2026 年 11 月 30 日移除 Gemini 3.8 Flash。另接官方付费 API 时，Gemini 3.8 Flash 的促销单价到 12 月 31 日是 Gemini 4 Hydrogen 的一半，2027 年起两者标准单价相同。

### Gemini 4 Hydrogen · 哪些活值得用它

| 适合的场景 | 思考档位 | 实际怎么用 |
| --- | --- | --- |
| 每日资讯整理、TrendPilot 提字段、短摘要、标签去重 | 低档 `low` | 适合高频且容易抽查的活。图片粗分类、视频抽帧初筛也可以先用低档。 |
| TrendPilot 专家作品逐篇总结、几篇资料综合、按要求写长稿 | 中档 `medium` | 把原文依据、结论和适用条件一并交付。算力中心新请求默认用中档。 |
| 图片细节、小字与图表、视频镜头定位、逐镜提示词 | 中档 `medium` | 小字先裁切，快动作先补关键帧。导演方案定稿后，Gemini 4 Hydrogen 负责批量写提示词、素材清单和生成参数。 |
| 查日志、改小功能、批量执行已经确定的修复 | 中档 `medium` | 给明确目标、输入和验收方式。复杂架构或跨平台疑难根因交给 Gemini 4 Neon、Gemini 4 Carbon 做判断。 |
| 对比相互矛盾的资料、检查多镜头衔接、复核复杂图文 | 高档 `high` | 中档读到了材料但推断不到位时再升档。需要导演取舍或做高成本终审时，转 Gemini 4 Neon 高档。 |
| 高档反复漏掉关键因果、约束或异常边界的少数任务 | 最高档 `xhigh` | 先补齐证据和上下文，再针对难点复核一次。长材料反复查证也可用 Gemini 4 Carbon 高档。 |

文字 API 可直接传 `model: "gemini-4-hydrogen-low"` 等完整型号。识图与视频抽帧在识别接口传 `effort`。低、高、最高档取决于账号是否开通，低档不可选时用中档。需要限定只用 Gemini 4 Hydrogen 时，首轮可传 `fallback_model: false`。

### Gemini 4 Neon 和 Gemini 4 Carbon 的档位怎么选

| 模型和档位 | 适合的场景 | 怎样验收 |
| --- | --- | --- |
| Gemini 4 Neon 低档 `low` | 同一个 Gemini 4 Neon 对话里压缩草稿、整理讨论和归并简单备选 | 沿用已定的创作要求，核对关键决定是否遗漏。新建此类任务可直接用 Gemini 4 Hydrogen。 |
| Gemini 4 Neon 中档 `medium` | 口播结构、普通分镜、多套方案的优劣比较 | 交付可选方案、取舍理由及关键约束，便于当场修改。 |
| Gemini 4 Neon 高档 `high` | 多人走位、复杂镜头衔接、对外报告审稿和重要决策 | 给每处判断标明依据、受影响镜头或段落以及修改方法。 |
| Gemini 4 Neon 最高档 `xhigh` | 高档复核后仍有关键冲突的少数镜头或方案 | 只重查难点和关联材料。需要反复查证或排除多个假设时可交给 Gemini 4 Carbon。 |
| Gemini 4 Carbon 低档 `low` | 按现成清单核对报告、分镜或修复方案 | 逐条标明通过、待确认及依据，适合沿用已有的 Gemini 4 Carbon 对话。 |
| Gemini 4 Carbon 中档 `medium` | 多步骤推理、比较多版方案、核对跨章节约束 | 给出结论、证据和未解决项。指定 Gemini 4 Carbon 时省略档位用中档。 |
| Gemini 4 Carbon 高档 `high` | 疑难根因、长材料交叉核验、昂贵镜头的疑点终审 | 列出反例、触发条件和可验证的复核步骤。只把关键疑点和相关材料送进去。 |

### 把选型用到日常流程

| 流程 | 模型怎么接力 | 交付时检查 |
| --- | --- | --- |
| 每日新闻与公告 | 先用搜索工具取官方原文、报道和发布日期，再用 Gemini 3.8 Flash 低档或 Gemini 4 Hydrogen 低档整理。有矛盾的条目单独交给 Gemini 4 Hydrogen 中档。 | 每项有原文链接和时间。发布时间与事件日期分开，无法确认的结论标明原因。 |
| TrendPilot 专家作品 | Gemini 4 Hydrogen 低档批量提取标题、观点和引用，Gemini 4 Hydrogen 中档逐篇总结。跨作品归纳遇到证据冲突时，用 Gemini 4 Neon 中档复核。 | 关键观点能定位到原文，区分作者观点、可复用做法和适用条件。 |
| 素材入库与镜头检索 | Gemini 4 Hydrogen 低档批量打标签。小字、快动作或需要精确定位时，先裁图、加密取帧或截短片段，再用 Gemini 4 Hydrogen 中档。 | 有素材编号、片段时间范围、识别依据和待确认项。涉及对白时补转写或带音轨的输入。 |
| 短剧、漫剧与口播编导 | 把目标、时长、参考和人物约束交给 Gemini 4 Neon 中档出分镜。复杂场景用 Gemini 4 Neon 高档定走位与衔接。定稿后用 Gemini 4 Hydrogen 中档批量写提示词，再走 Flow 和 Lyria。 | 逐镜核对叙事作用、起止动作、方向和时长。Gemini 4 Neon 高档做生成前终审，疑点才交 Gemini 4 Carbon 高档复核。 |
| 算力中心开发与故障排查 | Gemini 4 Hydrogen 中档先整理日志、配置和发生条件。跨平台、疑难并发或多处改动用 Gemini 4 Neon 高档定位，复杂疑点用 Gemini 4 Carbon 高档复核。 | 定位到可验证的根因，记录修改和验证步骤。由 Gemini 4 Hydrogen 执行已明确的修改与测试。 |

### 资讯、文字与运维

| 场景 | 建议模型与思考档 | 升档条件和使用要点 |
| --- | --- | --- |
| 搜新闻、找公告 | Gemini 3.8 Flash 低档（过渡）／Gemini 4 Hydrogen 低档 | 先取实时来源，输出链接和日期。报道冲突、需要核对政策原文时用 Gemini 4 Hydrogen 中档，重大结论用 Gemini 4 Neon 中档复核。 |
| 去重、分类、表格字段与 JSON 提取 | Gemini 4 Hydrogen 低档 | 只返回要求的字段。另接付费 API 做大批量时可验证 3.5 Flash-Lite 最低档，复杂或嵌套字段用 Gemini 4 Hydrogen 中档。 |
| 短摘要、翻译、标题与文案改写 | Gemini 4 Hydrogen 低档 | 长材料、多种语气或需要保留细微含义时用 Gemini 4 Hydrogen 中档。 |
| TrendPilot 专家作品逐篇总结 | Gemini 4 Hydrogen 中档 | 输出原文依据、做法和适用条件。跨作品归纳且证据冲突时，用 Gemini 4 Neon 中档复核。 |
| 跨资料研究、决策与对外报告 | Gemini 4 Hydrogen 中档 → Gemini 4 Neon 中档 | Gemini 4 Hydrogen 先整理来源和事实。Gemini 4 Neon 复核推断、反例与风险，重要决策可升 Gemini 4 Neon 高档。 |
| 查日志、例行排障、改文档或小功能 | Gemini 4 Hydrogen 中档 | 先定位出错环节并做最小改动。 |
| 跨 Windows／Ubuntu 故障、重试、并发与架构 | Gemini 4 Neon 高档 → Gemini 4 Carbon 高档 | 用 Gemini 4 Neon 定位根因、评审方案。长时间深度核验用 Gemini 4 Carbon 高档，再用 Gemini 4 Hydrogen 中档执行明确的修改与测试。 |

### 图片、视频理解与素材库

| 场景 | 建议模型与思考档 | 升档条件和使用要点 |
| --- | --- | --- |
| 素材入库、标签、去重、常见物体与风格 | Gemini 4 Hydrogen 低档 | 标签批量处理。重复素材和相同分析要求优先复用已有结果。 |
| 小字、票据、字幕截图、品牌与产品细节 | Gemini 4 Hydrogen 中档 | 先裁切放大。要求逐字准确时先做文字识别并复核关键字段。 |
| 图表、多个参考图和画面一致性 | Gemini 4 Hydrogen 中档 → Gemini 4 Neon 中档 | 让模型标明对应图片、差异位置和判断依据。资料复杂且需综合判断时才升档。 |
| 视频概述、场景分类和素材粗筛 | Gemini 4 Hydrogen 低档 | 用抽帧初筛。背景自动分析固定取 4 帧。 |
| 镜头定位、快动作、字幕与对白 | Gemini 4 Hydrogen 中档 | 算力中心手动每段最多取 8 帧且没有音轨。瞬时细节先加密关键帧。对白可另做转写，或走支持原生音视频的官方 API。 |
| 素材挑选、能否承接上下镜头、穿帮复核 | Gemini 4 Hydrogen 中档 → Gemini 4 Neon 中档 | Gemini 4 Hydrogen 先列候选与可用时间段。会影响镜头衔接时，交给 Gemini 4 Neon 对照参考帧和时间码。 |

### 视频编导与交付

| 场景 | 建议模型与思考档 | 升档条件和使用要点 |
| --- | --- | --- |
| 选题角度、开头钩子、口播结构 | Gemini 4 Neon 中档 | 需要批量给备选时用 Gemini 4 Hydrogen 中档扩写，关键反转或情绪节奏用 Gemini 4 Neon 高档定稿。 |
| 人物动机、故事因果、悬念与转折 | Gemini 4 Neon 高档 | 先定观众视角、信息揭示和情绪变化，再拆分镜头。 |
| 普通分镜、机位建议与剪辑节奏 | Gemini 4 Neon 中档 | 先定每镜的叙事作用、景别、时长和切点。 |
| 多人走位、机位轴线、视线匹配和空间关系 | Gemini 4 Neon 高档 | 给平面布局、人物位置与参考帧，检查镜头切换后的方向和视线。 |
| 动作戏、多镜头连续动作、首尾帧衔接 | Gemini 4 Neon 高档 | 先锁动作起止、受力和连续性。方案定后由 Gemini 4 Hydrogen 中档编写逐镜提示词。 |
| 口播转分镜、模板或素材匹配、画面与声音配合 | Gemini 4 Neon 中档 | 素材与情绪节奏打架时用 Gemini 4 Neon 高档改关键镜头。 |
| 逐镜提示词、素材清单、生成参数和批量变体 | Gemini 4 Hydrogen 中档 | 复用已经定稿的人物、构图、时序和动作约束。 |
| 生成前终审、成片审片、返工取舍 | Gemini 4 Neon 高档 → Gemini 4 Carbon 高档 | 优先评审高返工成本镜头。需要反复查长材料和多套方案时，交给 Gemini 4 Carbon 高档或 Gemini 4 Neon 最高档复核。 |
| 生图、生视频和配乐 | Flow／Lyria，使用生成档位 | 草稿先用低成本档，入选镜头再加清晰度。选模型 ID 时读算力中心快照。 |

### 遇到失败时怎么处理

来源、关键帧、音轨或上下文缺失时，先补充输入。材料齐全后仍漏因果、关联或约束，再提高思考档位。

遇到账号未开通、额度耗尽或排队时，先查账号和派发状态。换更高档仍受同一账号的共享额度和账号权限约束。已发送、部分返回或结果未知时，按原请求编号查询。

对照测试时首轮传 `fallback_model: false`，用结果里的 `model` 确认实际型号。切换模型系列要用新对话，并把原始材料、已定约束和待查问题一起交接。

### 算力中心选型与计费

算力中心 API 可显式选择 Gemini 4 Hydrogen、Gemini 4 Neon 或 Gemini 4 Carbon 的文字模型。实际档位取决于账号已开通的选项。可使用 `gemini-4-hydrogen-low`、`gemini-4-neon-high` 或 `gemini-4-carbon-high` 等完整模型 ID。同时传 `effort` 时须与型号一致。首轮省略模型和档位使用 Gemini 4 Hydrogen 中档。没有已开通 Gemini 4 Hydrogen 低档的账号时，可选择 Gemini 4 Hydrogen 中档。切换模型系列要新建对话。

识图与视频抽帧使用同一个识别接口，支持 `effort` 和 `fallback_model`。需要准确跟随指定档位时，可关闭 Gemini 3.8 Flash 保底。Gemini 4 Neon 和 Gemini 4 Carbon 的文字请求可传整理好的识别结果、镜头笔记或转写。现有识图接口不接收 `model`。新闻搜索先由外部工具获取原文，再交给算力中心整理。

算力中心 CLI 使用账号的 Gemini Models 共享额度。下表是 Google Gemini 付费 API 标准价，单位为美元／百万 token，输出含思考用量。搜索调用、缓存和批处理另计。

| 模型 | 输入 | 输出 | 应用与价格期限 |
| --- | --- | --- | --- |
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 | 另接 Google Gemini API，用于高频简单处理。 |
| Gemini 3.8 Flash | $0.75 | $3.75 | 到 2026 年 12 月 31 日，2027 年起输入 $1.50、输出 $7.50。 |
| Gemini 4 Hydrogen | $1.50 | $7.50 | 算力中心常规主力。 |
| Gemini 4 Neon | $3.00 | $15.00 | 复杂编导、判断和复核。 |
| Gemini 4 Carbon | $2.00 | $10.00 | 到 2026 年 11 月 30 日，12 月 1 日起输入 $4.00、输出 $20.00。 |

### 怎么评估性价比

用同一批带标准答案或人工验收标准的样本，固定输入、取帧方式和输出要求。先比较低档和中档，再把未通过的难例交给高档。记录实际模型、一次通过率、等待与处理耗时、返工时间。视频编导还要记重生成的镜头数。

算力中心按 CLI 实际消耗观察账号的 5 小时与每周共享额度，并记录完成量。测试期间若有其他任务并行或发生额度刷新，额度变化难以归到单一模型。

另接 Google 付费 API 时，输入与输出按用量计算，输出用量包含思考。假设 1,000 单，每单固定 10,000 输入 token 与 500 计费输出 token，Gemini 4 Hydrogen 约 $18.75，Gemini 4 Neon 约 $37.50，Gemini 3.8 Flash 促销价约 $9.38，Gemini 4 Carbon 促销价约 $25.00、之后约 $50.00。示例按标准档估算，搜索、缓存、批处理和实际思考用量会影响费用。

每条验收通过的成本等于首轮调用、后续复跑和人工返工的费用之和除以通过数量。定期抽查低档结果，有明显漏项时只升档复核相关任务。

### 指定 Gemini 4 Neon 高档的文字请求

先准备包含导演方案与分镜的 `storyboard_text`，沿用上文的 Python 公共准备。

```python
chat = call("/api/conversations", {"title": "复杂场景编导终审"})
rid = str(uuid4())
print("请求编号:", rid, flush=True)
print(call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto",
    "prompt": "核对下列分镜的走位、视线、轴线与动作衔接，逐镜指出需要修改的地方。\n" + storyboard_text,
    "model": "gemini-4-neon-high",
    "fallback_model": False
}, rid))
# 将 storyboard_text 设为你的导演方案与分镜文本，按原请求编号查询结果。
```

### 指定 Gemini 4 Carbon 的文字请求

```python
chat = call("/api/conversations", {"title": "Gemini 4 Carbon 深度核验"})
rid = str(uuid4())
result = call("/api/real-pool/chat", {
    "conversation_id": chat["id"], "account": "auto",
    "prompt": "请按重要程度列出这份方案的三个核验项。",
    "model": "gemini-4-carbon", "effort": "high",
    "fallback_model": False
}, rid)
print(result["request_id"], result["model"])
# effort 可改为 low 或 medium，后续用原请求编号查询最终结果。
```

- 不同模型的适用任务、档位和单价。[Google 模型选型与思考档位](https://aistudio.google.com/docs/choosing-a-model)
- 标准、批处理和搜索计费。[Google Gemini API 价格](https://aistudio.google.com/docs/pricing)
- CLI 套餐、档位和下线日期。[Antigravity 模型可用性](https://antigravity.google/docs/models?app=antigravity#reasoning-model)
- 5 小时和每周共享限额。[Antigravity CLI 额度说明](https://antigravity.google/docs/cli/commands/usage/)
- 原生视频、音轨与取帧选项。[Google 视频理解](https://aistudio.google.com/docs/video-understanding)

## Gemini · 上传图片并识别

**`POST /api/real-pool/uploads/{请求编号}/{序号}/{扩展名}`** · 传图片原始字节。序号从 0 开始；返回 index、extension、sha256。

**`POST /api/real-pool/image-analysis`** · 传 account: "auto"、prompt、attachments（上传回执数组），请求头沿用上传路径中的编号。返回 request_id、status 和 conversation_id。

可传 1～4 张 JPG、PNG、WebP，单张最多 8 MiB、合计 16 MiB、单张不超过 2400 万像素。只校验时用 `POST /api/real-pool/image-analysis-validate`，参数相同，返回 `valid`、`submitted: false` 和 `image_count`。

**复制识图请求 · 默认只校验**

```python
files = [Path("产品.png"), Path("场景.jpg")]
rid = str(uuid4())
print("请求编号:", rid, flush=True)
attachments = [
    call(f"/api/real-pool/uploads/{rid}/{i}/{f.suffix.lower().lstrip('.')}", file=f)
    for i, f in enumerate(files)
]
payload = {"account": "auto", "prompt": "请按顺序分别描述图片。", "attachments": attachments}
print(call("/api/real-pool/image-analysis-validate", payload, rid))
# 正式识图：print(call("/api/real-pool/image-analysis", payload, rid))
```

## Flow · 生成图片或视频

Flow 在提交前按编辑器实际段落核对提示词，保留原文的换行和空行。核对一致后才提交生成。输入或核对超时的任务会停在提交前，失败原因可用原任务编号查询。

**`POST /api/real-pool/flow`** · 传 kind、confirm: true 和 params。返回 request_id、status、task_id；submitted 表示已排队。

Flow 确认接收视频后释放派发名额，其他账号可以继续上传和提交。每个账号依次处理自己的任务，已提交的视频由工作台跟进并下载。提交后保留原 `task_id`，可继续提交新请求并轮询前一条的结果。

单图或单视频（`count: 1`，含参考素材）默认允许最多 3 次自动换号重试。仅在后台确认符合重试条件时继续，结果不明时暂停核对。在请求顶层传布尔值 `"auto_retry": false` 可关闭该任务的自动重试。查询和下载始终使用原 `task_id`，用 `retry.auto_retry` 核对设置，用 `retry.attempts` 核对尝试记录。同一请求编号沿用原设置。

在 `params` 里填写 `prompt`、`model`、`aspect` 和 `count`。视频额外填写 `resolution` 与 `duration`，图片不需要这两个字段。模型 ID 从快照取；账号和项目由工作台安排。积分按 Flow 实际报价计费，平台不设单笔积分上限。`max_credits` 可省略；旧调用携带该字段仍可兼容，但不会形成有效上限。

**复制纯文字生成请求 · 实际提交**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
payload = {
    "kind": "image",  # 生视频时改为 video，并补上 resolution 和 duration
    "confirm": True,
    "params": {"prompt": "一张清晨海边的照片", "model": "填写快照中的模型 ID",
               "aspect": "16:9", "count": 1}
}
print(call("/api/real-pool/flow", payload, rid))
```

### 画风 ID（可选）

上游节点在 Flow 生图或生视频请求中传入可选的 1～256 字符字符串 `params.style_id`，例如 `"style_id": "ink-wash-v1"`。画风 ID 是素材管理字段；用于生成的画风描述仍放在 `prompt`。

- 有值时，Flow 按原字符串记录，保留大小写和符号，不从提示词猜测，不改写、不替换。字段已提供但类型错误或值为空时明确报错。**完全省略 `params.style_id` 时，照常接收任务、生成并自动入库**；任务和素材来源不补填画风 ID。已有节点无需修改请求。
- 任务详情通过 `GET /api/tasks/{任务编号}` 的 `style_id` 回显原值；查工作台请求记录时位于 `task.style_id`。自动重试、换账号和多份产物沿用同一值，每个产物入库时写入对应来源的 `origins[].style_id`。
- 同一媒体文件去重时，保留各来源独立的画风 ID。自动分析标签与画风 ID 分开存储，任何分析结果不覆盖上游传值。
- 验收标准：从同一次提交分别读取任务详情及素材来源，两处均与上游传值逐字相同；未传时不补填，去重及重试不丢值。

若连接的是旧版后台，请省略此字段。

### Omni 视频模型

| 界面名称 | API 的 `params.model` | 支持的 `params.duration`（秒） |
| --- | --- | --- |
| Omni 1.1 Flash | `omni-flash-1.1` | 4、6、8、10 |
| Omni 1.2 Flash | `omni-flash-1.2` | 3～15 的任意整数 |

新调用推荐使用带版本号的 ID。旧 ID `omni-flash` 和 `omni-flash-2` 仍可调用，模型快照也可能沿用旧 ID。

**Omni 1.2 Flash · 15 秒纯文字生成（实际提交，可能消耗积分）**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
payload = {
    "kind": "video",
    "confirm": True,
    "params": {"prompt": "清晨海边，镜头沿海岸线平稳前推",
               "model": "omni-flash-1.2", "aspect": "16:9",
               "count": 1, "resolution": "720p", "duration": 15}
}
print(call("/api/real-pool/flow", payload, rid))
```

### 带参考素材

**`POST /api/real-pool/flow-references/auto/{素材 UUID}/{扩展名}`** · 逐个上传文件原始字节，返回 reference_id。上传、校验和提交使用同一个 Idempotency-Key，不填账号或项目。

| 模式 | 在 params 中添加 |
| --- | --- |
| 图生图 | `mode: "image-to-image"` 和 `reference_ids`（图片编号数组） |
| Veo / Omni 1.1、1.2 首帧或首尾帧 | `mode: "frames"`、`start_reference_id`，可选 `end_reference_id`（图片编号） |
| Omni 1.1 Flash 混合参考 | `model: "omni-flash-1.1"`、`mode: "ingredients"` 和 `reference_ids`（图片、MP4 编号数组） |

参考文件支持 JPG、PNG、WebP 和上述模式允许的 MP4，不接收独立音频。单文件最多 8 MiB、合计 64 MiB，编号 24 小时有效。只校验时发送 `POST /api/real-pool/flow-validate` 并设 `confirm: false`，返回 `valid`、`submitted: false` 和 `reference_count`。

**复制参考素材请求 · 默认只校验**

```python
rid = str(uuid4())
print("请求编号:", rid, flush=True)
file = Path("首帧.png")
ext = file.suffix.lower().lstrip(".")
ref = call(f"/api/real-pool/flow-references/auto/{uuid4()}/{ext}",
           request_id=rid, file=file)
payload = {
    "kind": "video", "confirm": False,
    "params": {"prompt": "镜头平稳前推", "model": "veo-fast", "aspect": "16:9",
               "count": 1, "resolution": "720p", "duration": 8,
               "mode": "frames", "start_reference_id": ref["reference_id"]}
}
print(call("/api/real-pool/flow-validate", payload, rid))
# 正式提交：将 confirm 改成 True，再调用 /api/real-pool/flow，可能消耗生成积分。
```

## 结果与下载

**`GET /api/real-pool/requests/{请求编号}`** · 查询文字或识图。status 为 completed 时，response 是回复；处理中继续查，失败或未知时读 note。

**`GET /api/real-pool/flow-requests/{请求编号}`** · 查询提交记录。task_id 始终是原任务号，task 是最新执行结果。

**`GET /api/tasks/{原任务编号}`** · 直接查询最新状态和下载路径，换号重试后仍用原任务号。

**`GET /api/tasks/{任务编号}/files/{序号}`** · 下载生成文件。使用任务的 downloads[].path，带同一密钥，返回文件字节。

**复制查询与下载**

```python
# rid 为发送时保存的编号。查文字或识图：
# print(call("/api/real-pool/requests/" + rid))
record = call("/api/real-pool/flow-requests/" + rid)
print(record)
if record.get("task_id"):
    task = call("/api/tasks/" + record["task_id"])
    if task:
        print(task["status"])
        for item in task["downloads"]:
            with HTTP.open(Request(BASE + item["path"], headers=AUTH), timeout=180) as res:
                with open(Path(item["name"]).name, "xb") as output:
                    while chunk := res.read(65536):
                        output.write(chunk)
```

已入库素材：`GET /api/library?page=1&limit=24` 查列表，`GET /api/library/{素材ID}` 查详情，`GET /api/library/{素材ID}/file` 下载原文件。

错误通常返回 `{"error":"原因"}`。`401` 检查密钥，`400` 检查请求参数，`404` 核对路径或编号，`409` 先查询原任务。`GET /health` 检查 18922 API 是否可访问，无需密钥。

## 现成素材查重入库

**`POST /api/library/import?filename=原文件名&sha256=校验值`** 使用同一工作台 Bearer 密钥。请求正文传一个文件的原始字节，使用 `application/octet-stream` 和文件大小 `Content-Length`。文件名需 URL 编码并包含扩展名。`sha256` 可省略，提供时服务会核对实际收到的内容。

支持 JPG、JPEG、PNG、WebP、GIF、BMP、AVIF、MP4、MOV、WebM、WAV 和 M4A。单个文件上限 1 GiB，超限返回 `413`。

- `201` 表示新增素材，`200` 并带有 `deduplicated: true` 表示复用已有素材。回复包含 `asset_id`、`media_type` 和下载地址 `path`。
- `asset_id` 是文件内容的 SHA-256。重复上传同一内容会返回相同编号。改名再上传会保留新原名供搜索，素材库仍只有一份内容。
- 上传前可用 `GET /api/library/{SHA-256}` 查重。若需要确认原文件也能取回，可对返回的 `url` 发起带 `Range: bytes=0-0` 的 GET。

```python
import hashlib
from urllib.parse import urlencode
from urllib.error import HTTPError

file = Path("待入库素材.mp4")
with file.open("rb") as stream:
    sha = hashlib.file_digest(stream, "sha256").hexdigest()

try:
    existing = call("/api/library/" + sha)
except HTTPError as error:
    if error.code != 404:
        raise
    query = urlencode({"filename": file.name, "sha256": sha})
    headers = {**AUTH, "Content-Type": "application/octet-stream",
               "Content-Length": str(file.stat().st_size)}
    with file.open("rb") as stream:
        request = Request(BASE + "/api/library/import?" + query, data=stream, headers=headers)
        with HTTP.open(request, timeout=190) as response:
            print(json.load(response))
else:
    print({"asset_id": existing["id"], "deduplicated": True, "path": existing["url"]})
```

## 音乐生成与音频

音乐新任务使用账号页优先顺序，提交生成前核对额度，并跳过无额度或不可用的账号。未出现在账号页的音乐账号按登记顺序补在后面。上游接收任务后继续查询原单。

使用同一统一 API 地址 `http://127.0.0.1:18922` 和工作台 Bearer 密钥。音乐走音乐服务，不改派 Gemini 或 Flow 图片/视频。

- `POST /api/music/jobs`：提交一次，返回 `202 {"job": {...}}`。
- `GET /api/music/jobs/{request_id}`：查询原任务，返回 `{"job": {...}}`；不存在返回404，不会生成。
- `GET /api/library?kind=audio&tag=钢琴`：筛选音乐标签。可叠加 `style_id` 精确匹配，参数请 URL 编码。
- Windows 导入素材与 Ubuntu 原有素材共用 `GET /api/library`。`q` 同时匹配原文件名、来源目录、提示词、原标签和已保存的 Gemini 或补充标签。同一个素材只返回一次。导入素材保留 `original_name`，可通过同一下载地址获取原文件。
- 内容维度可直接和关键词、媒体类型一起筛选。除主体、场景等原维度外，还可使用 `shot_size`（景别）、`viewpoint`（拍摄角度）、`composition`（构图）和 `movement`（运镜）。未分析的素材也可以按已有信息搜索。
- 音频物理文件按分类存到 `objects/audio/music`、`objects/audio/sound_effect` 或 `objects/audio/other`。素材 ID、检索和下载地址沿用同一套接口。
- `GET /api/library/{asset_id}`：素材详情；`GET /api/library/{asset_id}/file`：携带相同密钥下载原文件。

参数：`request_id` 为必填标准小写 UUID；`prompt` 为1～2000字；`model` 可选，默认 `lyria-fast`。如同时传 `Idempotency-Key`，必须与 `request_id` 一致。

| 模型 ID | 模型 |
|---|---|
| `lyria-fast` | Lyria 3.5 Fast |
| `lyria` | Lyria 3.5 |
| `lyria-pro` | Lyria 3 Pro |
| `lyria-pro-fast` | Lyria 3 Pro Fast |

`tags` 可选，字符串数组，最多30个，每个1～80字；`style_id` 可选，1～256字非空字符串。不传照常生成入库。手动标签及画风 ID 原样记录，不翻译、不改大小写、不删除首尾空格；同文件多来源保留各自原始记录。自动标签依据提示词中的已识别风格、情绪、乐器、用途和人声要求，以 `auto_tags` 返回；来源标记 `prompt_intent`，不是听音后的事实，不调用额外模型，不覆盖手动标签。未识别内容不猜测。标签规则不是自由文本语义理解，复杂否定句需要人工核对。

任务状态：`generating` 生成中；`ingesting` 入库中；`recovering` 重启后查询原任务；`completed` 已取得结果；`unknown` 结果待核对，禁止自动重发。查询结果含 `prompt`、`model` 和传入的可选字段；`result.clips` 保存音频/WAV地址、标签及画风 ID，`result.library` 保存入库回执（`asset_id`、`path`、`kind`、`ok`），`result.ingested` 为入库文件数，不是歌曲数。一首歌可能同时入库音频和 WAV。

同一编号、相同内容（包括标签和画风 ID）只生成一次；相同编号更换内容返回400。刷新、断线及后台重启后只查原编号，不重复生成。`completed` 不一定代表入库成功：若有 `ingest_error` 或 `ingested=0`，保留音频结果，不能为了入库再生成。上游音频链接可能过期，优先使用素材库下载路径。

生成时长通过提示词描述，并非精确时长参数。接口不接受指定账号、管理账号或一键就绪操作。统一 API 不开放 `/api/music/jobs/latest`；每个调用方自行保存自己的请求编号。

```python
# 沿用上文的 call、BASE、AUTH、HTTP。
from urllib.parse import urlencode
import time
rid = str(uuid4())  # 保存此编号；网络中断后只查询它
job = call("/api/music/jobs", {
    "request_id": rid,
    "prompt": "轻快钢琴背景音乐，无人声，适合口播，约30秒",
    "model": "lyria-fast",
    "tags": ["项目A", "片头备选"],
    "style_id": "STYLE-001"
}, rid)["job"]
for _ in range(180):
    if job["status"] in ("completed", "unknown"):
        break
    time.sleep(5)
    job = call("/api/music/jobs/" + rid)["job"]
print(rid, job)
# unknown 或仍在执行：保留 rid，稍后 GET；不要换编号重新生成。
if job["status"] == "completed":
    for i, item in enumerate(job["result"].get("library", [])):
        if item.get("ok") and item.get("path"):
            # path 为同一统一 API 的素材下载路径；格式见素材详情 mime。
            with HTTP.open(Request(BASE + item["path"], headers=AUTH), timeout=180) as res:
                with open(f"{rid}-{i}.audio", "xb") as output:
                    while chunk := res.read(65536):
                        output.write(chunk)
query = urlencode({"kind": "audio", "tag": "钢琴", "style_id": "STYLE-001"})
print(call("/api/library?" + query))
```

素材列表可叠加 `audio_category=music|sound_effect|other` 和 `duration_min`、`duration_max`。时长以非负秒数填写，含边界，可以只填写一端。使用这些参数时结果只包含音频。带时长条件时，只返回已读取时长的音频。例如 `GET /api/library?kind=audio&audio_category=music&duration_min=30&duration_max=65`。

音频素材详情中的 `duration` 是从入库文件读取的实际秒数，无法读取时为 `null`。`audio_category` 为 `music`（音乐）、`sound_effect`（音效）或 `other`（其他）。Flow Music 入库标记为音乐。这两个字段由素材库返回，生成时无需传入。

### 工作台内部兼容接口

18920 同源接口仍校验 Origin 和 `X-Workbench-Request: 1`。工作台使用同一持久化任务逻辑；`GET /api/music/jobs/latest` 仅供页面恢复，旧同步 `/api/music/generate` 保留兼容但不开放到统一 API。重启恢复依据提交时间、完整提示词、模型和唯一上游回执；标签及画风 ID 从本地原任务恢复，不作为再次生成的理由。

### 历史音乐标签补录

维护工具 `workbench/music_tag_backfill.py <素材库library.db>` 默认只预览，`--apply` 才写入。执行前在线备份数据库。仅按 Flow Music 音频的已保存提示词追加缺少的 `music_prompt` 标签，旧记录去除标题前缀；不重生成、不改文件或原始来源，不改手动标签及画风 ID，手动编辑过关键词维度的素材跳过。重复执行不重复添加。

### 音乐分类标签

`result.music_tags` 和 `result.clips[].music_tags` 返回分类自动标签：`music_style`（音乐风格）、`music_mood`（情绪）、`music_instrument`（乐器）、`music_use`（用途）、`music_voice`（人声）。`auto_tags` 平铺列表继续保留兼容。素材详情 `structured_tags` 使用相同字段；`GET /api/library?kind=audio&music_instrument=钢琴&music_mood=舒缓` 可叠加分类筛选，所有非英文参数需 URL 编码。分类不影响手动 `tags` 与 `style_id`。历史分类迁移只移动来源为 `music_prompt` 的已识别自动关键词，保留其他来源及手动编辑过的维度。
