# 币安返佣客户看板 (Binance Rebate Dashboard)

面向币安邀请返佣经营者的**本地优先**客户动态看板:把"该关心谁、什么时候联系、聊什么"变成打开页面就有答案。

数据闭环:`币安后台返佣 CSV 导入` → `只读 API 指标回补` → `本地 SQLite 存储` → `Web 看板展示与跟进维护`。所有数据留在你自己的电脑上,不上传任何外部服务器。

- **作者**: [Noah Ark (AOKE)](https://github.com/noahark)
- **X (Twitter)**: [@aoke_quant](https://x.com/aoke_quant)
- **开源协议**: MIT License

## 功能亮点

- **客户总览**:周期对比(7/15/30/90 天,本期 vs 上期)、返佣与交易额、活跃分层；默认统计基准为最新的完整 UTC 昨日，且支持在顶部自由选择任意历史基准日。
- **大户剔除联动**:一键排除前 1/3/5/10/20 大客户,实时重算并展示被剔除的体量。
- **唤醒关怀**:沉睡客户识别、跟进备注、星标重点、下次跟进日期,全部持久化在本地库。
- **导入与同步**:币安返佣 CSV 流式导入(严格校验 `好友ID（现货）` 表头与有效客户数，SHA-256 幂等防重)，主子账户自动关联;API 同步带限速与 429 退避。
- **严谨口径**:金额用 Decimal 高精度存储;现货/合约无可靠拆分时按钮置灰明示,绝不展示推断数据;UID 一键打码便于截图分享。

## 环境前提与验证环境

- **运行要求 (Runtime Requirements)**: Python 3.9+（标准 CPython，依赖仅包含 `requirements.txt` 中声明的 `fastapi`, `uvicorn`, `requests`, `python-multipart` 四项轻量依赖）。
- **实际已验证环境 (Verified Environment)**: macOS (Darwin arm64) + Python 3.9.6。所有核心单元与回归测试均在此环境下通过。代码采用标准跨平台库与 POSIX/Windows 兼容路径，提供了 Linux 与 Windows PowerShell 的操作指南，但本仓库尚未在本地建立跨操作系统与跨解释器版本的全自动化持续集成矩阵测试。

## 快速开始

### 1. 克隆与安装依赖

**macOS / Linux (Bash / Zsh):**
```sh
git clone https://github.com/noahark/binance-rebate-dashboard.git
cd binance-rebate-dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/noahark/binance-rebate-dashboard.git
cd binance-rebate-dashboard
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
*(注：若 Windows 提示脚本执行策略受限，可在 PowerShell 执行 `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process`)*

### 2. 运行模式选择

本看板支持两种启动方式：

#### 模式 A：无 Key 快速启动（本地基础版）
无需配置 `.env`（或保持 `.env.example` 占位符不变，系统会自动识别占位符并安全降级）：
```sh
python -m uvicorn main:app --port 8000 --host 127.0.0.1
```
> **无 Key 模式能力说明**：本模式绝非“只读白屏”，您可以完整使用 **CSV 客户档案导入、主子账户自动绑定归属、重点客户星标、沟通跟进日志与下次拜访日程持久化、大户剔除模拟**等所有核心功能；仅无法向币安 SAPI 请求自动化时序回补。

#### 模式 B：配置真实凭证（启用时序回补）
复制模板并填入你的币安只读 API Key 与 Secret：
```sh
cp .env.example .env
```
编辑 `.env`：
```ini
BK_KEY=你的币安只读API_KEY
BK_SEC=你的币安只读API_SECRET
```
> **安全声明**：在币安官网创建 API 时，**只需勾选「允许读取」**权限，绝不要开通交易或提现权限。凭证保存在本地 `.env`，代码库已默认 `.gitignore`。

启动服务：
```sh
python -m uvicorn main:app --port 8000 --host 127.0.0.1
```

### 3. 打开看板与健康检查

- **看板前端**: [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/)
- **健康检查 API**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

## CSV 导入规范与验收说明

### 导出与格式要求
- **获取路径**: 币安官网后台 → 「标准返佣」/「返佣中心」→ 导出好友列表或明细 CSV。
- **文件编码**: UTF-8 编码（完全兼容 Excel 直接导出的 UTF-8 with BOM / `utf-8-sig`）。
- **必需表头**:
  - `好友ID（现货）`：**必需列**。若缺少此列，导入接口将返回 HTTP 400 明确报错并拒绝执行任何写入，绝不产生脏数据。
- **可选表头**:
  - `好友子账户ID（现货）`：自动与主账户建立层级关联，免除人工合并统计。
  - `推荐码`：该好友绑定的邀请码。
  - `referee_note` 或 `附言`：币安后台的好友备注，自动同步为平台备注（永不覆盖本地手动备注）。
  - `注册时间`：格式如 `2026-09-01 10:00:00`。
- **口径说明**: CSV 主要用于流式建档和账户关联映射；由于后台导出金额存在切日统计差，具体的交易额与佣金数据统一以 API 结算时序回补为准。

**虚构 CSV 示例 (`sample.csv`)**:
```csv
好友ID（现货）,好友子账户ID（现货）,推荐码,referee_note,注册时间
900000001,,CODE_A,大客户张总,2026-09-01 10:00:00
900000002,900000003,CODE_B,量化团队李经理,2026-09-02 11:00:00
```

### 功能验收步骤
1. **健康检查验收**: 访问 `http://127.0.0.1:8000/health`，返回 `{"status":"ok", ...}`，确认 SQLite 本地库连接正常。
2. **导入数量验收**: 进入「数据导入与同步」页面，上传你的 CSV 文件，前端会实时反馈解析总 UID 数与新增客户数；刷新「客户中心」即可查看客户档案列表。
3. **覆盖度验收**: 配置 Key 后点击回补，任务后台串行执行并带有 429 智能退避；「返佣总览」将显示当前周期的完整数据覆盖度与对比环比。

## 目录结构

```
├── main.py              # FastAPI 入口:REST API + 静态前端挂载
├── binance_client.py    # 币安 SAPI 签名只读客户端(凭证从 .env 注入)
├── src/
│   ├── database.py          # SQLite + WAL,Decimal 高精度金额
│   ├── importer.py          # 返佣 CSV 流式导入(SHA-256 幂等与表头校验)
│   ├── dashboard_service.py # 看板聚合查询(动态昨日UTC/大户剔除/状态分层)
│   └── sync_service.py      # API 限速同步、占位符识别、退避、后台回补任务
├── dashboard/           # 纯静态前端(无构建步骤)
├── tests/               # unittest 测试套件(支持临时独立测试库)
├── schema.yaml          # 币安官方 Rebate API OpenAPI 参考
└── data/                # 本地数据库(已 gitignore,首次运行自动创建)
```

## 测试

```sh
python -m unittest discover -s tests -p "test_*.py" -v
```

所有核心单元测试与回归测试（包括动态日期计算、占位凭证拦截、无效 CSV 校验等）默认使用临时隔离数据库，全部离线通过。
联调测试默认跳过,按需通过环境变量开启:

| 环境变量 | 作用 |
|---|---|
| `TEST_CSV_PATH` | 指向一份真实返佣 CSV,启用导入幂等测试 |
| `TEST_UID` | 已入库的某个客户 UID,启用看板服务联调 |
| `TEST_UID_1` / `TEST_UID_2` / `TEST_CODE` | 你自己的被邀请人 UID / 推荐码,启用真实 SAPI 只读联调(需先配好 `.env`) |

## 常见问题 (FAQ)

- **Q: 启动时提示 8000 端口被占用？**
  - A: 启动命令指定其他端口即可：`python -m uvicorn main:app --port 8001 --host 127.0.0.1`，然后访问 `http://127.0.0.1:8001/dashboard/`。
- **Q: 为什么上传 CSV 提示缺少「好友ID（现货）」？**
  - A: 请确认上传的是币安返佣后台导出的标准好友明细 CSV。若用 Excel 编辑过，请以 `CSV UTF-8 (逗号分隔)` 格式另存为再上传。
- **Q: 为什么刚导入 CSV 客户列表没有佣金金额？**
  - A: 币安 CSV 导出仅用于建立客户名册与主子账户关系。交易额与返佣金额数据依赖币安 Affiliate Performance SAPI（只读接口）按 UID 拉取落库。该 API 为分钟级延迟结算口径，与币安网页后台月度/财务报表导出存在少许固定口径差值属正常现象；系统如实记录底层返回数据，不进行推断填充。
- **Q: 统计基准日期是如何确定的？**
  - A: 为了避免当日未结束交易引起的数据波动并确保周期对比口径闭合，本系统设计上统一以完整的 UTC 自然日为统计单元，默认动态以 UTC 昨日作为统计截止日（请求时动态计算，非启动时冻结）。您也可以在页面右上角通过「基准日 (UTC)」自由选择任意历史截止日进行周期回溯与复盘。

## 安全与隐私

- **凭证安全**: `.env` 已在 `.gitignore` 中，请保持本地文件权限（`chmod 600 .env`）；Key 仅需“允许读取”权限。
- **数据不出门**: 所有 CSV 与 SQLite 本地数据全部留在你自己的电脑上，永不上报任何云端服务器。
- **网络绑定**: 生产运行请务必绑定 `127.0.0.1`，不要绑定 `0.0.0.0`，确保仅限本机访问。

## 给 AI Agent 的开发者手册

详见 [AGENTS.md](AGENTS.md) —— 包含项目架构、数据流、API 清单、数据库结构及核心设计约束。
