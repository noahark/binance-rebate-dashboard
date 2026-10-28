# 币安返佣客户看板 (Binance Rebate Dashboard)

面向币安邀请返佣经营者的**本地优先**客户动态看板:把"该关心谁、什么时候联系、聊什么"变成打开页面就有答案。

数据闭环:`币安后台返佣 CSV 导入` → `只读 API 指标回补` → `本地 SQLite 存储` → `Web 看板展示与跟进维护`。所有数据留在你自己的电脑上,不上传任何服务器。

作者:[Noah Ark (AOKE)](https://github.com/noahark) · MIT License

## 功能亮点

- **客户总览**:周期对比(7/15/30/90 天,本期 vs 上期)、返佣与交易额、活跃分层
- **大户剔除联动**:一键排除前 1/3/5/10/20 大客户,实时重算并展示被剔除的体量
- **唤醒关怀**:沉睡客户识别、跟进备注、星标重点、下次跟进日期,全部持久化在本地库
- **导入与同步**:币安返佣 CSV 流式导入(SHA-256 幂等,重复导入不产生脏数据),主子账户自动关联;API 同步带限速与 429 退避
- **严谨口径**:金额用 Decimal 高精度存储;现货/合约无可靠拆分时按钮置灰明示,绝不展示推断数据;UID 一键打码便于截图分享

## 快速开始

```sh
git clone https://github.com/noahark/binance-rebate-dashboard.git
cd binance-rebate-dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入你自己的币安只读 API Key(BK_KEY / BK_SEC)
python -m uvicorn main:app --port 8000 --host 127.0.0.1
```

打开浏览器:
- 看板前端: http://127.0.0.1:8000/dashboard/
- 健康检查: http://127.0.0.1:8000/health

然后在「导入同步」页上传你从币安后台导出的返佣 CSV,再点同步回补指标即可。

**无 Key 也能跑**:不配置 `.env` 时系统以只读模式使用本地库,页面不白屏;只是无法做 API 回补。

## 目录结构

```
├── main.py              # FastAPI 入口:REST API + 静态前端挂载
├── binance_client.py    # 币安 SAPI 签名只读客户端(凭证从 .env 注入)
├── src/
│   ├── database.py          # SQLite + WAL,Decimal 高精度金额
│   ├── importer.py          # 返佣 CSV 流式导入(SHA-256 幂等)
│   ├── dashboard_service.py # 看板聚合查询(周期对比/大户剔除/状态分层)
│   └── sync_service.py      # API 限速同步、退避、后台回补任务
├── dashboard/           # 纯静态前端(无构建步骤)
├── tests/               # unittest 测试套件
├── schema.yaml          # 币安官方 Rebate API OpenAPI 参考
└── data/                # 本地数据库(已 gitignore,首次运行自动创建)
```

## 测试

```sh
python -m unittest discover -s tests -p "test_*.py" -v
```

默认全部离线可过。三个联调测试默认跳过,按需开启:

| 环境变量 | 作用 |
|---|---|
| `TEST_CSV_PATH` | 指向一份真实返佣 CSV,启用导入幂等测试 |
| `TEST_UID` | 已入库的某个客户 UID,启用看板服务联调 |
| `TEST_UID_1` / `TEST_UID_2` / `TEST_CODE` | 你自己的被邀请人 UID / 推荐码,启用真实 SAPI 只读联调(需先配好 `.env`) |

## 安全与隐私

- **凭证**:`.env` 已在 .gitignore,请保持 `chmod 600`;Key 只需"允许读取"权限
- **数据**:CSV、SQLite 库默认全部 gitignore;你的客户数据永不离开本机
- **网络**:请只绑定 `127.0.0.1`。服务含两个 SAPI 调试代理端点(`/api/invitee-performance`、`/api/code-performance`)且 CORS 全开——这是为本地调试设计的,**不要绑定 0.0.0.0 暴露到网络**,否则别人会借你的 Key 查数据

## 给 AI Agent 的开发者手册

见 [AGENTS.md](AGENTS.md) —— 架构、数据流、端点表、DB schema、约定与坑,一份读完即可上手改代码。
