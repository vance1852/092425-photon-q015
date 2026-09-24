# 电厂调度与能源分析与机组分析准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录电力市场基准电价、电厂与变电站设施、送出线路、燃料批次、发电计划和负荷情景，并保留机组巡检传感器统计分析准入流程。系统面向电价连续波动、关键送电送出线路恢复、电量调度和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 电力市场基准电价按结算日和来源修订登记，历史版本不会被覆盖；
- 电厂、储罐、终端与储能站设施建档，送出线路保存日能力、在途时间和损耗规则；
- 送出线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 燃料批次保留电源类型、牌号、数量、单位成本和接收时间，可计算加权燃料库存成本；
- 交易方提名支持载荷级幂等、优先级分配、燃料库存扣减和在途交接；
- 负荷情景保存电价变化、送出线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

机组分析准入子域位于 `plant_science` 包，负责机组巡检传感器的设备构建登记、不可变校准协议、测点分片导入、异常测点复核、统计任务租约、分析准入决定和审计报告。该子域不连接传感器硬件，只处理已经结构化的校准记录。

## 目录

- `src/power_dispatch/`：电价、设施、送出线路、燃料库存、提名、负荷情景、HTTP API 与离线验收；
- `src/plant_science/`：机组巡检传感器校准与统计分析准入；
- `src/equipment_booking/`：共享光谱仪与封测线的团队设备预约、冲突检测与周配额；
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API、并发唯一成功语义和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m power_dispatch.acceptance --workspace .
```

该命令会在内存数据库中登记六个结算日的峰谷电价，创建电厂、终端和送出线路，完成燃料库存入账、提名分配、送电及负荷情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

机组分析准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m plant_science.acceptance --workspace .
```

## 光电芯片研发协同服务

`src/photon_fab/` 提供光电芯片批次、光谱测量、科学计算、质量审批和审计的离线后台。SQLite 保存完整批次生命周期，角色权限覆盖操作员、工程师、质量人员和管理员；峰值波长、噪声 RMS、响应度、置信区间及良率计算均为确定性本地算法。

```bash
PYTHONPATH=src python3 -m photon_fab.acceptance
PYTHONPATH=src python3 -m photon_fab.api --database photon.sqlite3 --port 8080
```

HTTP 健康检查为 `GET /health`，登录、批次、测量和分析请求均支持 JSON；服务不访问外部网络，可在单个 Linux 应用容器中完成验收。

## 实验室设备预约服务

`src/equipment_booking/` 为多个伙伴团队共享光谱仪和封测线提供后台预约：

- 管理员维护团队（含每周配额分钟数，0 表示不限）、设备（光谱仪、封测线等）并可停用设备；
- 工程师只能为所属团队创建、取消或改期预约，管理员可跨团队操作；
- 半开区间 `[start, end)` 冲突检测：首尾相接允许，重叠返回 `409 conflict`；
- 配额按 UTC ISO 周分别计费，跨周预约按周截断；取消后配额立即释放；
- 预约开始（UTC）后普通用户不能取消或改期，管理员强制操作同样写入审计；
- 全部写操作进入哈希串联审计日志，`GET /audit/verify` 可离线校验顺序与内容完整性；
- 所有写事务使用 `BEGIN IMMEDIATE`，配合活跃预约的数据库级部分唯一索引，
  并发抢同一时段在真实多线程下保证恰好一个成功；
- 所有时间在入口处必须显式携带时区（`Z` 或偏移），统一归一化为 UTC。

```bash
PYTHONPATH=src python3 -m equipment_booking.acceptance
PYTHONPATH=src python3 -m equipment_booking.api --database equipment_booking.sqlite3 --port 8090
```

除 `GET /health` 外，请求通过 `X-Actor-Id` 携带操作者编号。主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/teams` / GET `/teams` | 管理员建档 / 列出团队 |
| PUT | `/teams/{id}/quota` | 设置每周配额分钟数 |
| GET | `/teams/{id}/quota?week=YYYY-Www` | 周配额用量与剩余分钟 |
| POST | `/users` | 管理员登记工程师（初始化时可匿名创建首个管理员） |
| POST | `/resources` / GET `/resources` | 设备建档 / 列表 |
| POST | `/resources/{id}/deactivate` | 停用设备（停用后不能新建预约） |
| GET | `/resources/{id}/schedule?start=&end=` | 查询时间窗内的设备排期 |
| POST | `/reservations` | 创建预约（可带 `idempotency_key` 与可选 `team_id`） |
| GET | `/reservations/{id}` | 查询单个预约 |
| POST | `/reservations/{id}/cancel` | 取消预约（需 `reason`） |
| POST | `/reservations/{id}/reschedule` | 改期 / 修改用途，变更前后均入审计 |
| GET | `/audit` / GET `/audit/verify` | 管理员查看审计事件 / 校验哈希链 |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、负荷情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。
