# Flow 下单腿预检改造计划

## Repository Research

- Flow 编辑页在 [FlowEditor.tsx](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/frontend/src/pages/flow/FlowEditor.tsx) 中已有 Save、Activate、Run Now 和右侧 [ExecutionLogPanel.tsx](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/frontend/src/components/flow/panels/ExecutionLogPanel.tsx)；Run Now 会先自动保存未保存修改，再打开执行日志。
- 后端执行入口是 [flow.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/blueprints/flow.py) 的 `/api/workflows/<id>/execute`，实际执行在 [flow_executor_service.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/flow_executor_service.py)。正式执行一定会调用 `create_execution()` 写入 `flow_workflow_executions`。
- 下单节点包括 `placeOrder`、`smartOrder`、`optionsOrder`、`optionsMultiOrder`、`basketOrder`、`splitOrder`。`NodeExecutor` 已负责参数解析、默认值、价格合法性、期权到期日/偏移/手数和篮子行解析。
- [FlowOpenAlgoClient](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/flow_openalgo_client.py) 同时封装了下单与只读接口。只读能力已经存在：
  - 合约存在性由 [quotes_service.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/quotes_service.py) 的 master contract token 校验保证；
  - 期权合约可由 [option_symbol_service.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/option_symbol_service.py) 或 [options_multiorder_service.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/options_multiorder_service.py) 的 `resolve_leg_symbol()` 解析；
  - 合约元数据包含 lot size、tick size、freeze quantity；
  - 资金来自 `funds()`，保证金来自 `margin()`，二者均为查询接口。
- 因此不需要新增页面，也不需要新增正式执行记录表。

## Files and Modules

- [services/flow_executor_service.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/services/flow_executor_service.py)：复用 `NodeExecutor` 的参数解析，增加预检专用的日志措辞和只读客户端接线。
- 新增 `services/flow_preflight_service.py`：实现只读预检客户端、逐节点/逐腿合约/行情/保证金/资金检查和日志组装。
- [blueprints/flow.py](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/blueprints/flow.py)：新增 `POST /api/workflows/<id>/preflight`，只返回日志，不创建 execution。
- [frontend/src/api/flow.ts](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/frontend/src/api/flow.ts)：新增预检 API 方法和返回类型。
- [frontend/src/pages/flow/FlowEditor.tsx](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/frontend/src/pages/flow/FlowEditor.tsx)：在现有工具栏增加“检查”按钮；复用自动保存逻辑和现有日志面板。
- [frontend/src/components/flow/panels/ExecutionLogPanel.tsx](file:///Users/gaobo/repositories/标注任务/swe/sources/5826-openalgo/frontend/src/components/flow/panels/ExecutionLogPanel.tsx)：区分“执行”和“预检”标题/状态文案，但不新增页面。
- 新增后端隔离测试 `test/test_flow_order_preflight.py`，并扩展现有前端日志面板测试。

## Implementation Steps

1. **实现只读预检服务**
   - 定义 `PREFLIGHT_ORDER_NODE_TYPES`，只枚举会新建订单的六类节点。
   - 为每个节点实例化现有 `NodeExecutor`，让它沿用正式执行的同一套参数解析和错误格式；预检客户端接管它原本会调用的下单方法。
   - 只读客户端显式屏蔽所有写操作（普通单、smart、options、multi-options、basket、split、modify、cancel、close），保证任何遗漏都会报错而不是默认下单。
   - `placeOrder`/`smartOrder`/`splitOrder` 使用最终 `symbol/exchange/quantity/pricetype/product/price/trigger_price` 检查。
   - `optionsOrder` 用 `get_option_symbol()` 解析当前行情下的到期合约；`optionsMultiOrder` 用 `resolve_leg_symbol()` 逐腿解析，自定义腿和模板腿都覆盖。
   - `basketOrder` 在 `NodeExecutor` 完成行解析后逐行检查。

2. **逐腿生成可拒绝原因**
   - 合约：master contract 中必须存在；期权腿必须能按当前 underlying LTP、到期日、strike/offset、CE/PE 解析出具体交易合约。
   - 行情：最终合约必须取得有效正数 LTP；期权 offset 解析失败时保留底层行情服务返回的具体原因。
   - 合约规则：校验数量为正、按 lot size 交易、价格符合 tick size、非 split 单笔数量不超过 freeze quantity；split 检查每笔分片和最大分片数。
   - 保证金/资金：先取账户可用资金，再用只读 margin API 计算节点篮子保证金；多腿节点按篮子整体计算以保留价差保证金优惠。资金不足时输出需要保证金、可用资金和差额。
   - 日志以节点和腿编号定位，例如“`optionsMultiOrder` 节点 / 腿 2 / NFO:... PE SELL qty=...：资金不足...”。

3. **新增预检 API**
   - 路由放在执行路由附近，沿用登录态校验。
   - API key 优先使用当前会话；没有时尝试读取该 workflow 已存储的 key；仍无则返回明确配置错误。
   - 先运行严格结构校验，让缺少触发节点、图结构不完整等问题也直接出现在预检日志中。
   - API 返回 `{status, message, logs, checked_legs, failed_legs}`；不调用 `create_execution()`、`update_execution_status()`，不改变 active 状态，不注册调度器/监控。

4. **接入编辑页和现有日志面板**
   - “检查”点击后先复用 `saveIfDirty()`，确保检查的是画布当前参数；保存本身不激活策略。
   - 打开现有 ExecutionLogPanel，状态显示 Checking / Check passed / Check failed。
   - 预检开始日志明确写出“只读检查：不会下单，也不会写入正式执行记录”。
   - Run Now 仍保持原有执行文案；预检与执行通过面板的 mode/kind 区分。

5. **测试与验证**
   - 后端用 monkeypatch/mock 构造合约、报价、资金、保证金结果，不依赖 MySQL、Docker 或真实券商。
   - 覆盖：正常腿、不存在合约、无有效 LTP、lot/tick/freeze 错误、资金不足、多腿中第 2 腿失败。
   - 安全断言：预检过程不调用任何 place/modify/cancel/close 服务，不增加 `FlowWorkflowExecution` 行数，不改变 workflow `is_active`。
   - 前端组件/类型测试通过；再运行相关 Flow 后端测试和前端定向测试。

## Dependencies and Considerations

- 预检复用正式执行器的节点参数解析，避免预检规则与真实下单规则长期漂移。
- margin API 的券商返回字段已统一收敛到 `total_margin_required`，实现时仍会兼容 `total_margin`、`totalMargin`、`reqdMrgn` 等嵌套/扁平字段。
-  Analyze/Live 模式下资金与保证金查询沿用当前 `FlowOpenAlgoClient` 的模式语义，使预检结果与点击 Run Now 时实际走的账户模式一致。
- 预检只覆盖新建订单节点；修改、撤单、全平仓不是“下单腿”，本轮不伪装成已检查。

## Validation

- `pytest test/test_flow_order_preflight.py test/test_flow_custom_legs.py test/test_flow_default_product.py -q`
- 如实现触及通用执行解析，再追加 `pytest test/test_flow_qa_regressions.py test/test_flow_workflow_validator.py -q`
- 前端定向执行 Vitest/TypeScript 检查（至少覆盖 ExecutionLogPanel 和 Flow API 类型）。
- 人工检查代码路径确认预检路由不导入 `create_execution`，只读客户端不导入下单服务。

## Risks

- **券商 margin API 不支持或字段差异**：将该腿标记为检查失败并提示“无法取得保证金/券商不支持”，而不是默许可下单。
- **非交易时段报价不可用**：直接报告无法取得当前 LTP/无法确定 ATM；这是预检无法确认“今天能跑”的硬阻塞，不伪造行情。
- **动态 webhook 变量无法静态确定**：沿用执行器的 unresolved variable 检查，具体指出字段和腿；不使用默认值冒充通过。
- **参数解析重构漂移**：不复制一套新的下单参数规则，而是调用现有 `NodeExecutor`；只读客户端只在最终 payload 阶段介入。
