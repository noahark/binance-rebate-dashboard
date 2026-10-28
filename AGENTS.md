# AGENTS.md — AI Agent 开发者手册

> 给接手本仓库的 AI agent / 开发者:读完这一份即可理解架构、跑起来、安全地改代码。
> 人类读者请先看 [README.md](README.md)。

## 0. 一句话

币安邀请返佣经营者的本地优先客户看板:FastAPI 后端 + SQLite + 无构建静态前端,数据全部留在本机。

## 1. 架构与数据流

```
币安后台导出 CSV ──import──▶ customers / subaccount_links / import_batches
                                     │
币安 SAPI(只读) ──sync(限速)──▶ customer_metrics / referral_metrics
                                     │
              dashboard_service(聚合) ──▶ REST API ──▶ dashboard/ 静态前端(fetch)
```

- `main.py` — FastAPI 入口(`main:app`),全部路由 + 静态挂载;`/api/dashboard/*` 业务接口,`/api/invitee-performance`、`/api/code-performance` 是透传币安 SAPI 的调试代理
- `binance_client.py` — HMAC-SHA256 签名只读客户端;端点 `/sapi/v1/affiliate/performance/invitee`(按 UID)与 `/code`(按推荐码);凭证构造注入,**代码里绝不写死 key**
- `src/database.py` — SQLite(WAL),金额一律 Decimal 字符串存储;`data/rebate_dashboard.db` 首次运行自动建库
- `src/importer.py` — CSV 流式导入,文件 SHA-256 幂等(重复导入返回 `already_imported`);平台备注永不覆盖手工备注;自动建立主子账户关联
- `src/sync_service.py` — 串行限速同步、HTTP 429/418 退避、已入库跳过、零值归一化(`empty_zero`)、后台线程回补任务(可取消);`get_binance_client()` 依次读环境变量 → `.env`
- `src/dashboard_service.py` — 周期对比(7/15/30/90 天本期 vs 上期)、大户剔除、状态分层、跟进/星标写回
- `dashboard/` — 原生 HTML/JS,无框架无构建,全部走 `fetch('/api/dashboard/...')`;`app.js` 内置 UID 打码开关

## 2. 跑起来

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # fastapi / uvicorn / requests / python-multipart
cp .env.example .env                # 填 BK_KEY / BK_SEC(币安只读 Key)
python -m uvicorn main:app --port 8000 --host 127.0.0.1
```

- 前端: http://127.0.0.1:8000/dashboard/ · 健康检查: `/health`
- **无 `.env` 降级**:只读本地库模式,页面完整可用,仅无法 API 回补——改代码时保持这个行为,不要让前端在白屏和报错里二选一
- 测试: `python -m unittest discover -s tests -p "test_*.py" -v` 默认全离线通过;联调测试靠 `TEST_CSV_PATH` / `TEST_UID` / `TEST_UID_1` / `TEST_UID_2` / `TEST_CODE` 环境变量开启,未设则 skip——**新增联调测试必须沿用这个模式,禁止硬编码真实 UID/推荐码/金额**

## 3. REST API 速查(main.py)

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/dashboard/overview?days=` | 总览(周期对比、覆盖度、口径声明) |
| GET | `/api/dashboard/customers?days=` | 客户列表(分页/筛选/排序) |
| GET | `/api/dashboard/customers/{user_id}` | 客户详情 + 跟进记录 |
| POST | `/api/dashboard/customers/{user_id}/star` | 星标开关 |
| POST | `/api/dashboard/customers/{user_id}/followup` | 写跟进备注/下次跟进日期 |
| GET | `/api/dashboard/care` | 唤醒关怀名单 |
| GET | `/api/dashboard/followups` | 跟进任务列表 |
| GET | `/api/dashboard/insights` | 洞察页聚合 |
| GET | `/api/dashboard/dynamics` | 动态页数据 |
| GET | `/api/dashboard/imports` | 导入批次列表 |
| POST | `/api/dashboard/imports/upload` | 前端上传 CSV 导入 |
| POST | `/api/dashboard/imports/update_csv` / `/default` | 扫描/导入默认 CSV |
| GET | `/api/dashboard/sync/status` | 同步任务状态 |
| POST | `/api/dashboard/sync/start` / `/cancel` | 启动/取消回补任务 |
| GET | `/api/invitee-performance` · `/api/code-performance` | SAPI 调试代理(透传,需 .env) |

## 4. 数据库表(src/database.py)

`customers`(主账户档案:平台备注/手工备注/星标) · `subaccount_links`(子→主归属) · `import_batches`(CSV 导入批次,SHA-256 幂等键) · `customer_metrics`(按 UID×日期区间的返佣/交易额,Decimal 字符串) · `referral_metrics`(推荐码聚合) · `customer_followups`(跟进记录) · `sync_jobs`(回补任务)

## 5. 铁律与约定(改了会被拒)

1. **真实数据零提交**:`data/`、`*.csv`、`*.xlsm/xlsx`、`.env`、日志全部 gitignore;示例数据只能用编造的占位值
2. **金额用 Decimal**,禁止 float 直算;展示格式化集中在 `format_decimal`
3. **不发明数据**:底层 API 没有的字段(现货/合约拆分、币种、多空方向)就如实置灰/标注,禁止推断填充
4. **凭证只读注入**:从环境变量/`.env` 读,代码、测试、文档里不得出现真实 key
5. **网络礼仪**:SAPI 调用串行 + ≥0.25s 间隔,429/418 退避重试;只读端点,禁止加任何写操作调用
6. **本地优先**:不要引入需要外部服务/云依赖的功能;前端保持无构建

## 6. 已知坑

- 调试代理端点 + CORS `*`:仅用于本机,部署时**必须绑 127.0.0.1**
- SAPI 只认主账户 UID,子账户查询返回空——归属关系靠 CSV 导入建立
- API 佣金为分钟级延迟结算口径,与后台导出存在固定差值,属正常
- 该 Affiliate Performance 接口未公开收录,字段可能变动;同步层已做逐账户容错,新增解析逻辑请先防御字段缺失

## 7. 仓库卫生

`.gitignore` 已覆盖:`.env`、`config/`、`data/`、`*.csv`、`*.xlsm`、`*.xlsx`、`*.log`、`logs/`、`.pytest_cache/`、`.venv/`、`node_modules/`。提交前 `git status` 自查一遍;若新增本地产物目录,先加 ignore 再提交。
