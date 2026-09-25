# 餐桌安全食品追溯服务

这是一个面向农产品监管部门、检测实验室和蔬菜配送企业的模块化后端，集中管理供应商、蔬菜批次、抽样检测、农残限值、运输链、风险处置、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 农产品档案：登记供应商、种植地、蔬菜批次和追溯标识。
- 检测业务：登记抽样、实验室结果、农残限值和风险判定。
- 运输追踪：记录装车、转运、到货、温度与异常处置。
- 风险协同：支持批次隔离、召回、监管公告和跨部门办理。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、农产品批次、抽样检测、运输追踪、风险任务和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  food/             农产品、检测、运输和风险处置服务
  complaints/       食品投诉登记、证据快照引用、分派限时处置与结案复核
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

## 食品投诉处置

投诉以独立记录登记（投诉编号、联系人姓名/电话、涉事食堂、事发时间、关联批次与配送单）。投诉状态机为：

`registered 已登记 → assigned 已分派 → processing 处置中 → reviewing 待复核 → closed 已结案`；处置中可进入 `supplementing 待补充材料` 后再回到处置中；复核退回或结案后证据被撤销进入 `reopened 复查重办`；重复投诉进入 `merged 已合并`。

核心规则：

- **证据快照**：引用检测证书、温度/配送记录、批次时，当场把来源数据（含嵌套样品、批次、温度点序列）固化为 JSON 快照并计算 SHA-256 哈希。批次随后被召回或状态变更不影响投诉持有的原始证据。
- **撤销保留 + 复查标记**：证书或温度记录被撤销时不删除快照，证据置为 `revoked`、`needs_review=1` 并进入复查队列；若投诉已结案则自动重开（`reopened`，累计重开次数）。
- **合并不吞记录**：重复投诉合并后状态为 `merged` 并指向主投诉，但各自的联系人、联系方式、时间线和材料保留在原记录上，主投诉列出全部独立联系人；已合并投诉只读。
- **补充材料**：区分 `request`（责任人发起）与 `submission`（投诉人/食堂提交），全程入时间线。
- **查询视图**：列表与详情返回当前责任人（`assignee`）、剩余时限（`remaining_hours`，负值即超期；结案后为 null）、证据完整度（批次/证书/温度三类齐全为 100 分，撤销证据不计分且单列待复查数）和结案理由（`close_reason`，重开后仍可查）。

接口前缀 `/api/food/complaints`：登记 `POST /`、列表（支持 `status`、`assignee`、`lot_id`、`overdue_only`、`include_merged` 过滤）`GET /`、详情（含证据、材料、时间线、主/从投诉关系）`GET /{id}`、引用证据 `POST /{id}/evidence`、撤销 `POST /{id}/evidence/{eid}/revoke`、复查销记 `POST /{id}/evidence/{eid}/review`、分派 `POST /{id}/assign`、开始处置 `POST /{id}/start`、要求补充材料 `POST /{id}/supplement-request`、提交补充材料 `POST /{id}/supplements`、提交结案 `POST /{id}/submit`、结案复核 `POST /{id}/review`、合并重复投诉 `POST /{id}/merge`。
