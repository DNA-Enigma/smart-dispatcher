# 12 部署（2 核 2G 云服务器）

面向真实用户的首次上云。本文只讲**部署事实与踩坑点**，不含业务说明
（业务见 [01](01-architecture.md)–[11](11-multi-agent.md)）。

结论先放前面：**装上能跑起来没问题，但有两条会直接影响用户，必须在放量前处理**——

1. **所有状态都在进程内存里**，重启即全丢（第 7 节逐条列出）。
   其中「已发放的子令牌全部失效」和「已批准的策略改进悄悄回退」两条，
   用户会当成 bug 报上来。
2. **必须单 worker**。任务状态与令牌登记簿都在进程内，多 worker 会出现
   「提交到 A 进程、查询打到 B 进程 → 404」。systemd 单元里已经钉死 `--workers 1`。

---

## 1. 安装（用锁文件，别裸装）

```bash
# 云服务器上，以部署用户身份
sudo mkdir -p /opt/smart-dispatcher && sudo chown smart-dispatcher: /opt/smart-dispatcher
git clone <repo> /opt/smart-dispatcher

cd /opt/smart-dispatcher
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock      # 全部精确钉版
.venv/bin/pip install --no-deps -e .            # 本项目自身

cp .env.example .env && chmod 600 .env          # 填密钥，见第 2 节
```

- **Python 版本**：`pyproject.toml` 要求 `>=3.11`（用了 `asyncio.TaskGroup`）。
  本仓库 `.venv` 用的是 3.12.3，`requirements.lock` 也是在这个版本上生成的。
  建议服务器装同一个大版本，别用 3.13+——锁文件里的 wheel 是按 3.12 选的。
- **`--no-deps` 不能省**：不加的话 pip 会按 `pyproject.toml` 的区间（`>=`）
  重新解析，把锁文件钉住的版本升降掉。
- **仓库位置定了就别挪**。本项目是 editable 安装，`dispatcher/core/settings.py`
  的 `REPO_ROOT` 由 `__file__` 上溯三级得出；把仓库挪走而没重装，
  契约文件（`config/`、`prompts/`、`schemas/`）与 `.env` 就全找不到了。
- 用 `requirements.lock` 装出来的环境**与本地/CI 完全一致**，
  它包含 dev 依赖（pytest / ruff），因此服务器上也能直接跑 `pytest -q` 复验。

---

## 2. 配置从哪来

**`.env` 是唯一来源**（`settings.py:37` 的 `env_file=REPO_ROOT / ".env"`，
用绝对路径，与 cwd 无关）。取值优先级是 pydantic-settings 的标准顺序：
**真实环境变量 > `.env` 文件 > 代码默认值**。systemd 单元用 `EnvironmentFile`
把 `.env` 读进进程环境，两条路读到的是同一份内容，不冲突。

`.env` 里放三类东西：密钥（`LLM_API_KEY`）、部署事实（`LLM_BASE_URL`、
`DISPATCHER_AUTH_TOKEN`、`DISPATCHER_TENANT/USER`）、进程自保上限
（`DISPATCHER_*`）。业务阈值一律**不在**这里——那些在 `config/routing.policy.yaml`。
分层规则见 [08-config-model.md](08-config-model.md)，不重复。

### 硬编码项盘点（已核）

| 位置 | 内容 | 上云是否要改 |
|---|---|---|
| `main.py:33` | `uvicorn.run(..., host="127.0.0.1", port=8000)` | 只在 `python main.py` 时生效；`-m uvicorn` 启动时走命令行参数，**不用改** |
| `openapi.yaml:32` | server `http://127.0.0.1:8000/v1` | **不改**。它是给本地调试看的；且 `tests/test_auth_contract.py:120-126` 钉住了这一条 |
| `settings.py:28` | `REPO_ROOT` = 源文件上溯三级 | 不用改，但决定了仓库不能挪（见第 1 节） |
| `pipeline.py:190,201` | sqlite 缺省路径 `REPO_ROOT/data/dispatcher.db` | 当前**用不到**（默认内存后端，见第 7 节） |

结论：**没有需要改代码才能上云的硬编码**。改动的都是命令行参数与 `.env`。

### `.env` 权限

实测本机 `.env` 是 `664`（同组、他人可读）。里面有 `LLM_API_KEY` 和主令牌，
服务器上必须 `chmod 600 .env` 并 `chown smart-dispatcher`。
另外注意 `.env` 与代码同目录，所以**不要**把 `/opt/smart-dispatcher` 开成
其他用户可读。

---

## 3. 进程管理

单元文件：[`deploy/smart-dispatcher.service`](../deploy/smart-dispatcher.service)（尚未部署）。

```bash
sudo cp deploy/smart-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-dispatcher
systemctl status smart-dispatcher
journalctl -u smart-dispatcher -f          # 跟日志
```

单元里几个刻意的选择：

- **`Type=simple` + 前台运行**。`uvicorn` 自己就是进程管理者，不需要
  `gunicorn`/`uwsgi` 再套一层——套了反而多一层信号转发，
  而 `Restart=always` 已经覆盖守护需求。
- **`EnvironmentFile=` 不带 `-` 前缀**，即 `.env` 缺失时服务**拒绝启动**。
  刻意的：`.env` 缺失时应用会以「鉴权关闭」启动，只在日志里留一条 ERROR，
  而那时任何能访问端口的人都能读全部任务快照和金融截图（`app.py:129`）。
  宁可起不来，也不要一个敞着门的服务。
- **`--workers 1`**，见文首。这是最容易被"优化"掉的一条，
  优化掉的后果是间歇性 404 与 401，且极难查。
- **`Restart=always` + `RestartSec=2`**：崩溃、被 OOM killer 杀掉、手工 kill
  都会拉起。注意这正是第 7 节「内存态丢失」会被用户感知到的路径。
- **`TimeoutStopSec=20`**：SSE 是长连接，不给上限的话
  `systemctl restart` 会一直等，最后被 systemd 强杀。
- **沙箱**：`NoNewPrivileges` / `PrivateTmp` / `ProtectHome` / `ProtectSystem=full`。
  没用 `strict`，因为当前不落盘、`full` 已经够；将来换 sqlite 后端时
  按文件里的注释加 `ReadWritePaths`。

---

## 4. 绑定地址、反向代理与 HTTPS

现状：默认只绑 `127.0.0.1:8000`（`main.py` 写死的是 `127.0.0.1`，
`uvicorn` 自己的默认 host 也是 `127.0.0.1`）。`docs/security-audit-20261007.md:65`
把这一点记为"对外暴露取决于部署"——这个部署就是现在。

**推荐：绑 `127.0.0.1`，前面放 nginx/caddy 终止 TLS。** 单元文件已经是这个配置。

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ---- SSE 必需，缺一条就等于没有实时流 ----
        proxy_buffering off;            # 不缓冲，否则事件攒着不发
        proxy_cache off;
        proxy_read_timeout 3600s;       # 默认 60s 会把空闲的 SSE 连接掐断
        chunked_transfer_encoding on;
    }
}
```

要点：

- **SSE 那三行是必需的**。少了 `proxy_buffering off`，nginx 会把事件攒在缓冲里，
  客户端看到的是「任务卡住，最后一次性收到一堆事件」；
  少了 `proxy_read_timeout`，心跳间隔（`sse_heartbeat_ms: 15000`，
  `config/routing.policy.yaml:429`）之外的空闲连接会被 60s 默认值掐断。
- **`X-Forwarded-*` 的作用有限，别指望它做鉴权**。uvicorn 的 `--proxy-headers`
  默认开启（实测 `Config.proxy_headers=True`，`forwarded_allow_ips` 默认
  `"127.0.0.1,::1"`），但它只影响日志里记录的客户端 IP 与 `request.url` 的 scheme；
  本服务的身份**唯一来源是 bearer token**（`dispatcher/interface/auth.py:24-27`），
  请求体/请求头里自报的 tenant/user 一律不采信。反代加的头伪造不了身份。
- **想直接绑 `0.0.0.0` 也可以**，但那样这三件事要自己扛：HTTPS 终止、
  限流（应用侧只有 LLM 的 QPS 闸，没有面向客户端的速率限制——
  审计表里「无速率与并发限制」一项未做）、以及 `/docs` 的公网可达性。
  用反代时 `/docs` 与 `/openapi.json` 会要求 bearer token，
  浏览器地址栏打不开（这是审计表里已记的决策点，不是本次引入的）。

---

## 5. 日志

**结论：日志只进 journald，不写任何文件，不需要 logrotate。**

核对结果：

- 全仓**没有** `FileHandler` / `RotatingFileHandler` / `addHandler`，
  也没有任何 `yaml.dump` / `write_text` 写文件的调用。
- `main.py` 用 `logging.basicConfig(stream=sys.stderr, level=INFO)`，
  配置在**模块顶层**（不在 `if __name__` 里）——按文档的
  `python -m uvicorn main:app` 启动时 uvicorn 是以 `main` 而非 `__main__`
  导入的，放进 `if` 块就等于没有应用日志。文件里的注释写明了这个坑。
- systemd 单元用 `StandardOutput=journal` + `StandardError=journal` +
  `PYTHONUNBUFFERED=1`，应用日志与 uvicorn 的 access log 都进 journald。

### 敏感数据核查

| 检查项 | 结论 |
|---|---|
| 日志里打 token？ | **否**。`auth.py` 全文没有任何 `log.` 调用；uvicorn 的 access log 只记方法/路径/状态，不记 header |
| 日志里打账单内容？ | **基本否**，有一处需留意：`openai_compat.py:337` 在 JSON 解析失败时以 WARNING 打印模型原始响应**前 800 字符**。那是模型输出（计划/决策 JSON），不是用户账单；但模型若把 prompt 里的内容原样吐回来，就会顺着这条落到日志里。排障必需，暂保留 |
| 密钥脱敏覆盖面 | `redact_secrets`（`settings.py:151`）按**值**抹除，已用在任务快照 notes（`state.py:219`）与计划 notes（`decomposer.py:521`），有测试（`tests/test_stage_notes.py:202`） |
| 启动日志含密钥？ | **否**。`describe_config`（`pipeline.py:1056`）只打版本号、档位名、路由 id，不含 `secret://` 引用值 |
| 响应体含密钥？ | 见下方遗留项 |

**一处已知缺口**（审计已记，本轮未改）：`Problem.detail` **未经**脱敏，
而 `detail` 会带上供应商响应体前 300 字（`openai_compat.py:225`）。
若供应商把请求头（含 `Authorization: Bearer <LLM_API_KEY>`）回显在错误体里，
密钥值会出现在**给客户端的响应体**中。客户端本来就有自己的 key，
所以不是第三方泄露，但它会进客户端日志/崩溃上报。
审计原文列在「错误体泄露」，状态未做——**不在本轮范围，留给 PM 排期**。

日志保留策略交给 journald（`SystemMaxUse=` 之类），不需要应用侧干预。

---

## 6. 健康检查与探活

用 `GET /v1/health`（`app.py:163`）：

```bash
curl -fsS http://127.0.0.1:8000/v1/health
```

**适合做探活**，理由：

- 它是 `PUBLIC_PATHS` 里唯一的路径（`auth.py:58`），**不需要 token**。
  这点很关键：探针要拿 token 才能活，就等于把万能钥匙散给负载均衡器
  （`auth.py:56` 的注释正是这个意思）。
- 不查数据库、不调 LLM，纯内存读取，开销可以忽略。
- 进程起来但 lifespan 没跑完时，`get_dispatcher()` 会抛
  `RuntimeError` → 500，探针能正确判失败。**这一点保证了它不只是"端口通"**。

**但它不能探测依赖是否可用**，两条限制要说清：

- 返回体里的 `tiers[].state` **恒为 `"closed"`**（`app.py:169` 里是字面量），
  不代表真实档位健康度。别拿它做告警。
- **缺 `LLM_API_KEY` 这类配置错误它照样回 200**。系统会看起来健康，
  而所有 LLM 调用都在失败。想覆盖这一点，探针得自己判
  `config_warnings` 是否非空（返回体里有这个字段），或者另加一个内部探针。

建议**只把 `/v1/health` 用作 liveness（进程活着）**，不要用它做 readiness
来触发重启——第 7 节的内存态让重启的代价比"慢一点"大得多。
nginx 上对应的是 `proxy_next_upstream` 之类的重试策略：本服务是**单进程**，
没有第二个上游可切，重试只会打到同一个进程，不如让它返回真实错误。

---

## 7. 进程内状态清单（重启即丢）

这是上云后用户会直接遇到的坑，逐条列出。**当前部署跑的是全内存后端**：
`lifespan` 调用 `Dispatcher.build()`（`app.py:100`），不传 config，
于是 `DispatcherConfig.state_backend` 取默认值 `"memory"`（`pipeline.py:81`）。
**全进程不落任何盘**（已用 grep 核对：全仓无写文件调用；`data/` 目录根本不存在）。

| # | 状态 | 存哪 | 重启后 | 可接受？ |
|---|---|---|---|---|
| 1 | **任务快照**（envelope、状态、节点进度、artifacts） | `InMemoryStateStore`（`pipeline.py:203`） | **全丢** | ⚠️ 受影响：客户端拿着 task_id 查询会 404。用户看到的是"任务凭空消失" |
| 2 | **事件流**（SSE 重放源） | 同上（`EventBus` 建在 state 上） | **全丢** | ⚠️ 断线重连的 `Last-Event-ID` 重放失效，任务本身也没了，同 #1 |
| 3 | **上传的媒体**（金融截图 blob） | `InMemoryMediaStore`（`pipeline.py:211`）——**写死，与 `state_backend` 无关** | **全丢** | ⚠️ `media_id` 变悬空引用，相关任务必然失败。另外它常驻内存：单张上限 10 MiB（`routing.policy.yaml:427`），缺省保留 1 天，100 张就是 1G——**2G 机器上这是最现实的内存风险** |
| 4 | **已发放的子令牌** | `TokenRegistry`（`app.py:144`，挂在 app.state） | **全丢** | ❌ **不可接受**：所有子令牌持有者当场 401，必须重新发放。`.env.example:27` 与 `docs/03-http-contract.md:59` 都声明了这一点，但它现在是**每次重启都发生**，不是理论风险 |
| 5 | **已批准的策略版本与补丁** | `InMemoryEvolutionStore`（`pipeline.py:193`） | **全丢** | ❌ **不可接受**：审批过的策略改进在重启后**静默回退**到 `config/routing.policy.yaml` 的内容（全仓无写回文件，已核对）。用户会认为"批准了没用" |
| 6 | **自进化的 RunLog / 待审批建议 / 拒绝率统计** | 同上 | **全丢** | ⚠️ 分析要从零重跑；`pending_count`、丢弃率归零 |
| 7 | **预算账本**（`/v1/usage` 的 spent/by_user/by_tenant） | `BudgetLedger`（`budget.py:68-70`） | **清零** | ⚠️ 跨重启的用量统计不成立。且审计已记「用量统计恒 0」，本就是不完整功能（`budget.py:60` 注明持久化属 M6） |
| 8 | **在执行的任务与其取消登记** | `Dispatcher._running` / `_cancels` | 随 #1 一起没 | ✅ 可接受：进程都没了，任务本就该失败 |
| 9 | **画像缓存** | `evaluator._cache`（`evaluator.py:113`） | 冷启动 | ✅ 可接受：只是重算，且审计记了它无上限无 TTL（另有账） |
| 10 | **商户分类缓存** | `handlers/bookkeeping/handler.py:203` 的 LRU | 重问 LLM | ✅ 可接受：多花几次调用，无数据损失。上界已在 `7b00704` 修过 |
| 11 | **媒体保护集 `_media_refs`** | `pipeline.py:167` 进程内 dict | 全丢 | ✅ 可接受**仅在单进程**下——审计表已按"接受单进程假设"记 |
| 12 | **SSE 连接名额** | `ConnectionLimiter`（`app.py:151`） | 归零 | ✅ 可接受：本来就应该归零 |
| 13 | **日历示例 handler 的事件** | `handlers/calendar/handler.py:29` 的 list | 全丢 | ✅ 可接受：示例实现，真 handler 在 M4 接入 |

### 一句话总结

**#1–#3 是"任务与数据"，#4–#5 是"凭据与配置"。后两条最难受，因为它们是静默的**——
前者用户会立刻看到 404，后者用户以为生效了其实没有。

### 能不能换成持久后端？

能，但**当前没有开关**——这是本次检查最重要的发现，需要 PM 决策：

`DispatcherConfig.state_backend` 支持 `"memory"` / `"sqlite"`
（`pipeline.py:186-203`，`SqliteStateStore` / `SqliteEvolutionStore` 都已实现
且有测试），但：

- `Settings` 里**没有** `state_backend` 字段，`.env.example` 里**没有**这一项，
  `lifespan` 调 `Dispatcher.build()` 时**不传 config**。
- 也就是说：**改 `.env` 换不了后端，只能改代码**。
- 即便换上 sqlite，**媒体仍然是 `InMemoryMediaStore`**（`pipeline.py:211`
  是写死的构造，不看 `state_backend`）——#3 不会跟着解决。

因此「让状态跨重启存活」是一次**代码改动 + 测试补充**，不是配置项。
按硬约束本轮不动业务代码，留作决策项。

---

## 8. 上线前 checklist

- [ ] `chmod 600 .env`，属主 `smart-dispatcher`
- [ ] `DISPATCHER_AUTH_TOKEN` 已填（**留空 = 鉴权关闭**，启动日志里会有 ERROR）
- [ ] 三个 `LLM_*_MODEL` 与 `LLM_BASE_URL` 已填，`LLM_API_KEY` 有效
- [ ] `.venv/bin/python -m pytest -q` 在服务器上跑一遍，460 全过
  （锁文件含 dev 依赖，这一步是可做的）
- [ ] systemd 单元用 `--workers 1`，且 `EnvironmentFile` 指对路径
- [ ] nginx 的 SSE 三行（`proxy_buffering off` / `proxy_read_timeout` / `chunked_transfer_encoding`）已加
- [ ] `journalctl -u smart-dispatcher` 能看到启动那行 `smart-dispatcher 启动：{...}`，且其中**没有** `DISPATCHER_AUTH_TOKEN 未配置` 的 ERROR
- [ ] 反代侧限制请求体大小与 `/docs`、`/openapi.json` 的暴露面
- [ ] 已和 PM 确认第 9 节的决策项，尤其是「重启丢状态」在放量前是否可接受

---

## 9. 待 PM 决策 / 遗留

1. **状态持久化（最高优先）**：见第 7 节末。要真正解决需要
   ①把 `state_backend` 接到 `Settings`（配置项，改动小）
   ②给媒体换持久实现（`MediaStorePort` 已有端口，缺实现）
   ③补"重启后子令牌/策略回退"的验收方式。
   本轮未做，因为它会改运行时行为，超出「不改业务代码」的约束。
2. **`Problem.detail` 未脱敏**：审计已记（「错误体泄露」），本轮未改。
3. **客户端速率限制缺失**：审计表「无速率与并发限制」未做。
   反代侧的 `limit_req` 是短期缓解。
4. **2 核 2G 的容量口径**：SSE 每个连接一个生成器 + 一个 50ms 轮询任务
   （`app.py:669`），缺省上限 100 条（`DISPATCHER_SSE_MAX_CONNECTIONS`）。
   100 × 20 次/秒 = 2000 次/秒的轮询，在 2 核上不可忽略。
   **建议先按 20–30 调低**，压测后再往上放；这个值改 `.env` 即可，无需改代码。
5. **审计「依赖无 lock 文件」一项已由本次完成**，
   请 PM 在 `docs/security-audit-20261007.md` 的整改对照表里回填状态与提交号。

---

## 相关文档

- 配置分层与优先级 → [08-config-model.md](08-config-model.md)
- 媒体存储与保留策略 → [05-media.md](05-media.md)
- 认证与主/子令牌 → [03-http-contract.md](03-http-contract.md)
- 安全审计与整改对照 → [security-audit-20261007.md](security-audit-20261007.md)
