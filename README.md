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
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

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

### 共享设备预约

多个伙伴团队共享光谱仪（`spectrometer`）和封测线（`packaging_line`）。`src/photon_fab/schedule.py` 与 `storage_schedule.py` 提供预约后台：

- 管理员管理团队、资源，并通过 `PUT`（POST `teams/{id}/quota`）设置团队每周配额（分钟，UTC 周一为周界）；
- 工程师只能为**所属团队**创建、取消或变更预约；操作员无预约权限；管理员可代任意团队操作并可改写已开始的预约；
- 区间采用半开语义 `[starts_at, ends_at)`，全部时间必须带时区并归一化为 UTC；首尾相接不算冲突，重叠返回 409；
- **预约开始后普通用户不能取消或变更**，仅管理员可以；
- 取消（软删除，状态置 `cancelled`）与时段变更都保留旧值/原因和操作者的审计记录（`GET /audit`），取消后释放时段与周配额；
- 跨周边界的预约按重叠分钟分别计入各周；`GET /quota` 返回配额、已用、剩余与是否超额；
- 并发预约的唯一成功语义：进程内写锁串行化 + SQLite `BEGIN IMMEDIATE` 事务内冲突检测，同一资源同一时段的并发请求恰好一个成功（已由多线程与跨文件连接测试覆盖）。

预约相关接口（Bearer 令牌认证）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/teams` / `/resources` | 管理员创建团队 / 资源 |
| POST | `/teams/{id}/quota` | 管理员设置周配额 |
| POST | `/teams/{id}/members` | 管理员将用户编入团队（一人一队） |
| GET | `/teams` / `/resources` | 列出团队 / 资源 |
| POST | `/bookings` | 创建预约（`resource_id`、`starts_at`、`ends_at`，UTC） |
| GET | `/bookings` | 按 `resource_id`/`team_id`/`starts_after`/`starts_before` 查询 |
| POST | `/bookings/{id}/cancel` | 取消（需 `reason`），开始后仅管理员 |
| POST | `/bookings/{id}/change` | 变更时段，重新做冲突与配额校验 |
| GET | `/quota` | 周配额用量统计（`team_id`、`week_of` 可选） |
| GET | `/audit` | 预约/团队/资源审计事件 |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、负荷情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。
