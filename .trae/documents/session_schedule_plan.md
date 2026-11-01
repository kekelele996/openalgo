# 场次感知的定时（下一次可开始 / 必须停止时刻）实施计划

## 需求结论

Flow 与 /python 托管策略目前只按「星期几 + 固定 HH:MM」排 APScheduler cron：
- 节假日、周末 cron 照样触发，靠运行时闸门跳过（Flow `is_within_market_hours`、python `scheduled_start_strategy`）；
- 特别时段（Muhurat 周日 14:20 开盘）当天，09:15 的 start cron 会提前把进程拉起；
- 提早收盘/特别时段当天，固定 stop（15:30/16:00）晚于实际收盘，enforcer 明确不在钟点边界停止，策略在收盘后继续挂着；
- MCX 节假日夜市（17:00–23:55）当天，09:00 start 提前 8 小时启动、15:30 stop 在夜市开盘前就停掉；
- 界面「下次运行」取的是 cron 数学结果，会显示在已经结束/不存在的场次上；
- 日历查不到时（表不存在/DB 错误）日历函数吞异常返回 None，与「当天休市」无法区分。

目标：新增一个共享的场次计划解析，算出**下一次可开始时刻**与**必须停止时刻**两个具体 IST 绝对时间，标明**采用的是哪一场**（常规/特别/夜市、交易所、节假日名称），由每日重排的一次性任务执行；算不出来则**不装任务、不启动**并把原因写回既有的计划配置与执行记录。**仅覆盖 Flow + /python，不动 /strategy 模块；不新增页面。**

## 现有代码研究

- `database/market_calendar_db.py`
  - `get_effective_session_window(date, exchange)` 已是单一事实源（CRYPTO 24/7 → SPECIAL_SESSION → 节假日夜市窗口 → 休市 None → 周末 None → 默认时段），但异常时吞掉返回 None，休市与故障不可区分；不返回节假日描述。
  - 2026 种子含 Muhurat 2026-11-08（周日 14:20–15:30/15:40）、多个 NSE 节假日 MCX 夜市 17:00–23:55。
- `services/flow_scheduler_service.py`
  - `FlowScheduler.add_workflow_job`：interval/daily/weekly/once 全部装裸 Cron/Interval/DateTrigger；`execute_workflow_scheduled` 在触发后读 `get_market_hours_config` 闸门，跳过时只打 debug 日志、不留执行记录；`reconcile_scheduler_jobs` 启动时按 start 节点重建任务。
  - start 节点字段：`scheduleType/time/days/executeAt/intervalValue/intervalUnit/marketHoursOnly/marketHoursStart/marketHoursEnd/marketHoursExchange`（闸门默认开）。
- `blueprints/python_strategy.py`
  - `SCHEDULER`（内存、IST）已有全局任务：00:01 `daily_trading_day_check`、每分钟 `market_hours_enforcer`、死进程回收。
  - 每策略两个 cron：`start_{id}`/`stop_{id}`（`schedule_strategy`，2003 行）；`scheduled_start_strategy`（1624）只挡全天休市，`is_trading=True 但 before_market`（Muhurat 上午）会误启动；`scheduled_stop_strategy` 固定按用户时刻停止；`is_within_schedule_time`（1770）已做用户窗口∩场次窗口，但只用于 enforcer 恢复路径。
  - 状态面：`get_schedule_status`（2874）、`/api/strategies`（3027）、`/schedule/<id>`（2478）、`/start/<id>`（2302，armed 文案是「next trading day at HH:MM」粗算）；配置为 `strategy_configs.json`，已有 `paused_reason/paused_message`。
- Flow 执行记录：`flow_db.create_execution/update_execution_status`（状态 pending/running/completed/failed），前端 `ExecutionLogPanel.tsx` 渲染；FlowWorkflowExecution 无迁移负担（状态是字符串列）。
- 前端：Flow `ConfigPanel.tsx` start 节点（市场时段开关/起止/Calendar）；`SchedulePythonStrategy.tsx`（已有一句「stop at whichever comes first」的承诺但后端未兑现）、`PythonStrategyIndex.tsx` 状态行。
- 测试范式：`test/test_python_strategy_exchange_aware.py`（sqlite :memory: + StaticPool + patch now）、`test/test_flow_market_hours.py`。

## 文件与模块

**新增**
- `services/session_schedule.py`：共享场次计划解析（核心，纯函数 + 日历读取）。
- `test/test_session_schedule.py`：核心解析单测。

**修改**
- `database/market_calendar_db.py`：新增严格版窗口解析 + 场次描述（不动现有函数签名/行为）。
- `services/flow_scheduler_service.py`：每日重排、具体时刻任务、阻断原因与 skipped 执行记录。
- `blueprints/flow.py`：激活/保存时按计划装任务；新增 schedule-preview 接口。
- `blueprints/python_strategy.py`：start/stop 改为每日重排 DateTrigger；配置写回 next/must-stop/场次标注；preview 接口；状态文案。
- 前端：
  - `frontend/src/components/flow/panels/ConfigPanel.tsx`（start 节点显示下次可开始/必须停止/场次，阻断警告）；
  - `frontend/src/components/flow/panels/ExecutionLogPanel.tsx`（新增 skipped 状态样式）；
  - `frontend/src/pages/python-strategy/SchedulePythonStrategy.tsx`（原页内预览卡片）；
  - `frontend/src/pages/python-strategy/PythonStrategyIndex.tsx`（原状态行展示具体时刻/场次/阻断原因）；
  - 对应 api/types 文件小改。
- 测试：扩展 `test_flow_market_hours.py`（或新建 `test/test_flow_session_arming.py`）、`test_python_strategy_exchange_aware.py`。

## 实施步骤（依赖序）

1. **日历层严格解析**（market_calendar_db）
   - 抽出内部 `_resolve_window(query_date, exchange)`（不吞异常），现有 `get_effective_session_window` 保持包一层 try/except 的旧行为。
   - 新增 `describe_session_window(date, exchange)`：返回 `{start_ms, end_ms, is_special, kind, description, exchange}`，`kind ∈ regular|special|evening|crypto`，`description` 取 Holiday 描述（如 "Diwali Laxmi Pujan (Muhurat Trading)"）；全天休市/无场次返回 None；基础设施异常向上抛。
   - 规则：TRADING_HOLIDAY 且该 exchange 有显式 open 窗口 → `evening`（如 MCX 夜市）；SPECIAL_SESSION → `special`；普通工作日 → `regular`；CRYPTO → `crypto`。

2. **共享场次计划**（services/session_schedule.py）
   - `SessionPlan` 数据类：`exchange, session_date, start, end`（IST aware datetime）、`effective_start, effective_end`（与用户 HH:MM 窗口求交）、`next_start`（≥now 的下一次可开始）、`must_stop`（该场次必须停止）、`is_special, kind, session_label, blocked, reason`。
   - `resolve_plan(exchange, user_start, user_stop, weekdays, now=None, schedule_type="daily")`：
     - 自今天起逐日（上限 ~14 天）查 `describe_session_window`，要求「星期几被用户选中 ∧ 有场次 ∧ 用户窗口与场次窗口有交集」；
     - CRYPTO：24/7，只受用户窗口/days 约束；
     - `next_start`：今天且在有效窗口内 → now（interval 再做对齐）；未到 → effective_start；已过 → 下一场；
     - `must_stop`= effective_end = min(用户停, 场次收)；跨午夜窗口用绝对毫秒天然支持；
     - 任何日历层异常 → 抛 `CalendarUnavailable`（调用方据此 fail-closed）；
     - 有场次但窗口永不相交 → 返回 `blocked=True, reason="window_misses_session"`；14 天内无选中日场次 → `blocked=True, reason="no_session_in_days"`。
   - `plan_for_flow_trigger(data, now=None)`：处理 once/daily/weekly/interval；`marketHoursOnly=False` 时返回纯 cron 数学结果（`kind="unrestricted"`，不阻断、must_stop 取用户值或 None），保持旧语义；interval 返回对齐后首个落点 + end_date=effective_end（复用现有 `_next_aligned_start` 思路）。
   - 提供 `format_ist(dt)`、`session_label` 文案（如「MCX 夜市场次 17:00–23:55（Holi，NSE/BSE 休市）」「NSE 特别场次 14:20–15:30（Diwali Muhurat）」）。

3. **/python 每日重排**
   - 新增 `arm_strategy_session(strategy_id, now=None)`：读 config（exchange/schedule_start/schedule_stop/schedule_days），调 `resolve_plan`：
     - 成功且今天有场次：装/替换 `start_{id}` = DateTrigger(effective_start, IST)、`stop_{id}` = DateTrigger(effective_end, IST)；今天窗口已过则装下一交易日的具体时刻；
     - `CalendarUnavailable`：移除 start/stop 任务，写 `paused_reason="calendar_unavailable"` + 文案，**不启动**；
     - blocked：移除 start 任务（保留 stop 作为安全任务，若策略正在跑），写原因；
     - 写回配置 `next_start_iso/must_stop_iso/session_label/schedule_note`（供列表与原 schedule 页展示）。
   - `schedule_strategy()` 保存后调用 arm；00:01 全局任务改为「重排所有 scheduled 策略」（原全天休市停止逻辑保留为 arm 的一部分）；每分钟 enforcer 在 `calendar_unavailable` 时重试 arm（缓存命中，成本低），并继续承担恢复/兜底停止。
   - `scheduled_start_strategy` 触发时再解析一次：非 open 状态（before_market/after_market/休市/算不出）一律不起（修 Muhurat 上午误启动）。
   - `/schedule/<id>` 响应与新增 `GET /python/api/schedule-preview` 返回 plan；`/api/strategies` 增加 `next_start/must_stop/session_label/schedule_blocked_reason`；`get_schedule_status` 与 `/start` armed 文案改用具体时刻。

4. **Flow 每日重排**
   - `FlowScheduler.init` 增加一个全局单例 cron（id `flow_session_rearm`，00:05 IST）遍历 active workflow 的 start 节点重排；`reconcile_scheduler_jobs` 启动恢复后逐工作流 arm 一次。
   - 新增 `arm_workflow_session_job(workflow)`（放在 flow_scheduler_service，供 blueprint 与重排任务共用）：
     - daily/weekly：替换 `flow_workflow_{id}` 为 DateTrigger(next_start)；
     - interval（marketHoursOnly 开）：IntervalTrigger(start_date=窗内首个对齐点, end_date=must_stop)；次日由全局任务重建成下一场；
     - once：executeAt 落在有效窗口内才装；否则 blocked；
     - marketHoursOnly 关：维持现有裸 cron（plan kind=unrestricted）。
   - 激活/改触发（`flow.py _register_trigger`）：plan blocked 或 `CalendarUnavailable` → 抛 ValueError，激活回滚（沿用 `_rollback_activation`），前端收到明确错误，不允许「亮着但永不跑」。
   - `execute_workflow_scheduled` 闸门跳过/算不出时：`create_execution(status="skipped")` + `update_execution_status("skipped", error=原因文案, completed_at=now)`，执行记录面板可见；正常执行路径不变。
   - 新增 `POST /flow/api/schedule-preview`（登录态，与其他 flow 路由一致），入参为 start 节点 data，出参 plan JSON。

5. **前端（原页面内展示，不新增页面）**
   - Flow ConfigPanel start 节点：市场时段区下方展示预览（下次可开始、必须停止、采用场次与交易所）；blocked/算不出时红色警告；激活按钮失败走既有 toast 错误通道。
   - ExecutionLogPanel：status 映射加 skipped（灰色「已跳过」，title 显示原因）。
   - SchedulePythonStrategy：交易所/时刻/天数变化时调 preview，原卡片内显示「下次可开始 …，必须停止 …，采用 … 场次」；保存响应含 plan；blocked 时禁用保存或显示错误（与现有错误提示一致）。
   - PythonStrategyIndex：状态行副标题显示 next_start/must_stop/场次；calendar_unavailable 显示「日历不可用，未启动」。
   - 不引入新路由/新页面；api 客户端函数与 types 增补字段。

6. **测试（全部 sqlite 内存、不依赖 MySQL/Docker/外网）**
   - `test_session_schedule.py`：普通工作日；NSE 节假日 NSE 无场次/MCX 夜市 17:00 开始且 must_stop=min(用户停,23:55)；Muhurat 周日（选中 sun 才可开始，未选则跳过该天）；用户 09:00 开盘前 → next_start=14:20（Muhurat）；15:30 后 → 次日；用户窗口与场次无交集 blocked；CRYPTO 24/7；日历异常 → CalendarUnavailable。
   - Flow：daily/weekly 装出的 DateTrigger 时间 == 计划时刻；interval end_date == must_stop；blocked 激活抛错且不留任务；闸门跳过产生 skipped 执行记录；marketHoursOnly=false 保持裸 cron。
   - /python：arm 写出的两个具体任务时刻正确（特别场次 stop 被截到 15:30）；calendar_unavailable 时无 start 任务、有 paused_reason、不启动；Muhurat 上午触发不误启动；/api/strategies 新字段存在。
   - 运行：`uv run pytest test/test_session_schedule.py test/test_flow_market_hours.py test/test_python_strategy_exchange_aware.py test/test_python_strategy_edge_cases.py test/test_flow_lifecycle_guards.py -q`，并对改动的 tsx 跑 `npm run test`（ConfigPanel/ExecutionLogPanel/Schedule24x7 相关）与 `npx tsc -b`、biome。

## 依赖与注意事项

- 所有时刻内部统一 IST aware；APScheduler trigger 显式 `timezone=IST`（Flow cron 目前未带时区，本次新增的重排任务与 DateTrigger 都显式带上；不重写无关路径）。
- 任务幂等：重排一律 replace_existing；DateTrigger 时间已过时不入任务，改算下一场。
- Flow jobstore 为 SQLAlchemy 持久化：DateTrigger/IntervalTrigger 可 pickle；全局重排任务用固定 id，reconcile 逻辑里 `rsplit("_",1)` 解析 workflow id 的既有代码不受影响（不新增带后缀的工作流 job id）。
- /python 配置文件新增字段向后兼容（缺失即 None）；旧 cron 任务在首次 arm 时被同名替换。
- enforcer/daily check 的「全天休市停止」安全语义保留；本次只把「钟点边界」从固定 cron 变成场次窗口的具体时刻。
- `marketHoursOnly` 关闭的 Flow 与历史行为完全一致，不强制套日历。

## 风险与应对

- 重排任务在 00:05 跑，跨零点场次（end 落在次日）：窗口用绝对毫秒，stop 任务装绝对时间不受日期翻转影响；重排按 session 的 start 日期归属。
- APScheduler 停机错过重排：启动 reconcile 对每个 active 对象立即 arm 一次；/python 每分钟 enforcer 兜底重试。
- 日历表不存在（全新安装）：严格解析抛 CalendarUnavailable → fail-closed 不启动，与现有「异常按周末」的宽松默认不同，但仅作用于选择了具体交易所日历的定时入口；CRYPTO 不受影响。
- 前端构建环境不可用：以现有测试范式 mock 接口，至少保证后端与单测；前端改动保持最小并跑 tsc/biome。
