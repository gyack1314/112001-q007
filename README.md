# 园区算力容量日事实工程

算力调度团队每天需要按园区回答一个问题：**可用容量还有多少，能不能承诺给行业项目？**
本项目从三类来源的合成输入——容量台账（安装规模）、预留单（已承诺份额）、运行事件
（停机/恢复，可能迟到、撤销或更正）——按 **（园区, 资源类型, 日期）** 重算容量、预留、
停机与可用量的日事实，并管理已发送日报的版本。

要避免的三个典型错误：

1. **重复承诺**：直接用安装规模减当天停机，会把已预留资源再次对外承诺
   → 可用量 = 安装容量 − 预留承诺 − 日终在停，三个分量各自按业务规则求值；
2. **负容量 / 重复回补**：迟到的恢复事件若按增量打补丁，会让历史日报出现负容量或重复回补
   → 一切日事实从事件日志**整体重算**，在停数不允许为负，超出在停的恢复被截断并登记冲突；
3. **静默取乐观值**：不同来源状态冲突时挑一个好看的数
   → 冲突全部显式登记；阻塞型冲突使可用量**不给数值**，警告型冲突保留数值但标注可靠性存疑。

## 核心语义

### 双时间口径

每条事件携带两个时间：

| 字段 | 含义 |
| ---- | ---- |
| `effective_at` | 业务生效时间：事实属于哪一天/哪一时刻 |
| `recorded_at` | 知晓时间：系统何时得知；缺省等于 `effective_at`。迟到事件晚于 `effective_at` |

**按历史日期重算** = 业务日期（`effective_at` 落在哪天）+ 知晓时点（`--as-of`，只纳入
`recorded_at <= as-of` 的记录）。同一业务日期在不同 as-of 下可得到不同的事实版本——
这正是迟到事件的正确处理方式：历史不改写，视角可重算。

### 日事实口径

每行日事实包含：

| 字段 | 口径 |
| ---- | ---- |
| `installed_capacity` | 当日日终生效的安装容量（台账同一生效时刻取值冲突则不给数值） |
| `reserved_qty` | 当日日终处于承诺状态的预留单数量合计 |
| `downtime_qty` | 当日日终仍在停机的数量（驱动可用量） |
| `downtime_peak_qty` | 当日在停峰值（含前日结转），呈现"当天曾有多少暂不可用" |
| `available_qty` | `installed_capacity − reserved_qty − downtime_qty`；为负表示超承诺（不截断，暴露缺口）；阻塞型冲突存在时为 `—` |
| `status` | 正常 / 警告 / 阻塞 |
| `inputs` | 构成本行事实的输入事件 id 清单（决策依据） |

### 预留单：生效与释放遵循业务状态

预留单以 `reservation_id` 标识，每次状态上报是一条事件，日终时点最新一次上报决定当日状态：

- 计入承诺（从可用量扣减）：`approved`、`active`；
- 不计入：`submitted`、`pending`、`released`、`cancelled`、`rejected`、`expired`；
- 未标注状态的预留单**按已生效处理**——宁可少报可用量，也不把已承诺资源再次对外承诺；
- 无法识别的状态：保守计入承诺并登记 `RESERVATION_STATUS_UNKNOWN` 警告；
- 状态上报本身可以迟到（`recorded_at` 晚于 `effective_at`）：迟到撤销只影响当前重算，
  不改写已发送日报。

### 运行事件：撤销与更正

- `revoke`（撤销）：`target` 指向的事件自知晓后起从有效事件集中剔除，相应期间的
  当前重算不再包含它（如误报停机）；
- `correct`（更正）：`target` 指向的事件被更正版本取代，`effective_at` 重述生效时间，
  `value` 缺省沿用原值；
- 撤销/更正只能指向业务事件（capacity/reservation/outage/outage_recovery），
  指向不存在或已撤销的目标会登记冲突，不静默生效；
- 同一事件 id 出现内容不同的版本：**全部剔除**并登记阻塞型冲突（不静默挑选）；
  内容一致的重投视为幂等重试，只计一次。

### 冲突级别

| 级别 | 语义 | 对日事实的影响 |
| ---- | ---- | ---- |
| 阻塞 | 无法可靠判断 | `available_qty` 不给数值，状态为"阻塞" |
| 警告 | 数值存疑或超承诺 | 数值照常给出，状态为"警告" |
| 提示 | 信息性 | 不影响状态 |

| 种类 | 级别 | 说明 |
| ---- | ---- | ---- |
| `CAPACITY_CONFLICT` | 阻塞 | 容量台账同一生效时刻存在多个取值 |
| `CAPACITY_MISSING` | 阻塞 | 有业务活动但无有效容量台账 |
| `EVENT_ID_CONFLICT` | 阻塞 | 同一事件 id 出现内容不同的版本 |
| `RECOVERY_EXCEEDS_OUTAGE` | 警告 | 恢复数量超过在停数量（疑似重复回补），超出部分已截断 |
| `OVERCOMMITTED` | 警告 | 承诺+停机超出安装容量，可用量为负 |
| `RESERVATION_STATUS_UNKNOWN` | 警告 | 预留单状态无法识别，已保守计入 |
| `REVOKE_UNKNOWN_TARGET` / `CORRECT_UNKNOWN_TARGET` / `CORRECT_OF_REVOKED` / `DUPLICATE_CORRECTION` / `INVALID_TARGET_KIND` | 警告 | 异常事件流，未静默生效 |
| `OPEN_OUTAGE` | 提示 | 截至最后一条事件仍有未恢复停机 |

### 已发送日报：不可变快照

`send` 把某一报告日期的日事实固化为版本化快照（含数据截止时点与每行的输入事件清单），
**已发送版本永不修改**——它们是当时的决策依据。同一日期可再次发送，版本递增，历史版本
全部保留。因此"按历史日期重算"（`facts`）与"查看已发送日报"（`report`）各自给出明确的
事实版本，互不回写。

## 快速开始

运行环境为 Python 3.11 或更高版本，仅使用标准库。检查基础语法：

    python -m compileall src

运行全部测试（含输入契约检查）：

    python -m unittest discover -s tests

用合成数据演示完整场景（迟到恢复、重复回补、撤销、更正、容量冲突、超承诺、日报版本）：

    python -m src.cli demo

常用命令（仓库根目录下）：

    # 按业务日期重算日事实（当前视角）；--as-of 裸日期表示当日结束
    python -m src.cli facts --date 2026-04-05 --as-of 2026-04-08

    # 发送日报（留存不可变快照）
    python -m src.cli send --date 2026-04-05 --as-of 2026-04-05

    # 查看已发送日报（缺省最新版本，--version 指定历史版本）
    python -m src.cli report --date 2026-04-05 --version 1

    # 查看冲突清单 / 已发送版本列表
    python -m src.cli conflicts --as-of 2026-04-30
    python -m src.cli list

默认读取 `data/events.json`，日报快照写入 `state/reports.json`，可用
`--data` / `--state` 覆盖。

## 输入数据格式

`data/events.json` 为 JSON 数组，每行一条事件。契约：每行必须有 `kind` 与
`effective_at`（见 `tests/test_inputs.py`）；校验失败立即报错（带行号），不静默跳过脏数据。

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `kind` | 是 | `capacity` / `reservation` / `outage` / `outage_recovery` / `revoke` / `correct` |
| `effective_at` | 是 | 业务生效时间（ISO 8601） |
| `park` | 业务事件必填 | 园区 |
| `resource` | 否 | 资源类型，缺省 `default` |
| `value` | 视类型 | 数量：capacity/reservation/outage/outage_recovery 必填；correct 缺省沿用原值 |
| `recorded_at` | 否 | 知晓时间，缺省等于 `effective_at`（迟到事件晚于生效时间） |
| `reservation_id` | 预留单必填 | 预留单标识，多次状态上报构成其生命周期 |
| `status` | 否 | 预留单业务状态，缺省按 `approved`（已生效）处理 |
| `target` | revoke/correct 必填 | 目标事件 id |
| `id` | 否 | 事件 id，缺省按行号自动分配 `E1..En`；撤销/更正通过 id 引用 |
| `source` / `note` | 否 | 来源系统与备注，冲突明细中展示 |

测试资料为合成园区与事件：`data/events.json` 包含 P01（迟到恢复、误报撤销、数量更正）、
P02（gpu/cpu 两类资源、扩容、超承诺、重复恢复、迟到撤销预留）、P03（容量台账冲突）。

## 项目结构

    data/events.json      合成输入事件流
    src/
      models.py           事件模型与业务常量
      conflicts.py        冲突模型、种类与级别
      loader.py           输入加载与校验（脏数据立即报错）
      engine.py           日事实引擎：事件解析 + 分量计算 + 可用量合成
      reports.py          已发送日报的不可变版本化存储
      cli.py              命令行：facts / send / report / conflicts / list / demo
    tests/                unittest 测试（引擎、事件机制、预留生命周期、日报、输入契约）
