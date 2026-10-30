# 人工 A/B 单轮控制（IS-06/07/08）— 实现说明与设计逐条对照

- 分支：`5w/sim-manual-one-shot-20261030`（基线 `6141300`）；位置：CV 模拟器进程内 `/_ctl/*` + `simcore/control/`
- 设计依据（冻结，优先级自高向低）：
  1. Owner 冻结要求（A 1:240 / B 1:60 与 62 停点不变；运行中/初始化中/已暂停/B 等待确认/停止处理中不得切换；切换只改选择；暂停≠停止；所有改状态的人工操作必须确认/取消；一次点击启动、无第二个开始按钮；62 停点不得被普通继续或旧/重复确认绕过；刷新/重连/进程恢复不得自动开始第二轮）
  2. V1.1 正式设计 03《A/B 模拟器人工单轮运行设计》§1–§6
  3. E02《三域设计说明与差异草案 V0.1》SIM 节
  4. T04《模拟器侧正式数据装载清单》计数（318 事件 / 62 停点 / 原 10 条）
- 本轮**未**接真实目标、**未**向任何运行中的系统发送事件；验证范围仅为容器内本地替身（`tests/control/`，`--network none`）。

## 1. 模块结构

| 文件 | 职责 |
|---|---|
| `simcore/control/config.py` | `ControlConfig` 与 `config_from_env()`；获准目标、冻结计数、确认码有效期等全部显式配置，缺获准目标即拒绝启动 |
| `simcore/control/machine.py` | 单轮状态常量、显示名、**纯函数**守卫表 `transition_error()`、重启恢复规则 `recovered_state()` |
| `simcore/control/store.py` | 文件持久化：`<dir>/{A,B}/state.json`（原子写）、`results.jsonl`、`unknown.jsonl`（只追加）、`<dir>/audit.jsonl`；加载即执行重启恢复 |
| `simcore/control/challenge.py` | 二次确认门：一次性、60 s、绑定操作、绑定状态指纹 |
| `simcore/control/plan.py` | 只读装载 A240 / B060 预切 62 段剧本并逐项核对计数、顺序、manifest 一致性 |
| `simcore/control/outcome.py` | 三值判定 `ACCEPTED / KNOWN_REJECTED / UNKNOWN` 与窄拒绝合取判据 |
| `simcore/control/target.py` | 目标身份校验（逐段 URL 比对 + `/readyz` 的 `data_domain`/`artifact`/`ready`） |
| `simcore/control/module.py` | `ControlModule`（人工操作入口）、`Runner`（唯一轮次线程）、`build_control_router()` |
| `simcore/control/runlock.py` | 全局运行锁：进程内 `threading.Lock` + `fcntl.flock` 文件锁 |
| `simcore/control/page.py` | 最小状态页（单 HTML、无框架、无构建） |
| `cv/pusher.py` `send_once()` | 单次发送、不重试、完整响应留痕（状态码、响应头、正文全文、字节数、截断标记、耗时、目标、尝试时刻） |
| `cv/app.py` | 装配：共享运行锁给 `/_sim/cv/emit(-raw)` 与原生 scenario，挂载 `/_ctl` 路由 |
| `simcore/ipguard.py` | `/_ctl*` 与 `/_sim/*` 同级纳入管理端来源白名单 |

## 2. 端点合同

所有 `/_ctl/*` 需 `X-Sim-Admin-Token` 且受 `SIM_ADMIN_ALLOW_CIDRS` 限制；`GET /_ctl/`（页面本身无状态、无密钥）仅受来源限制。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/_ctl/` | 状态页（HTML） |
| GET | `/_ctl/status` | 当前模式、状态与显示名、轮次、计划与目标、进度、B 停点、计数、最后技术结果、未知清单、允许操作、运行锁 |
| GET | `/_ctl/results?n&mode` | 逐事件技术结果（含响应正文全文） |
| GET | `/_ctl/unknown?mode` | 未知结果锁存与核清留痕 |
| GET | `/_ctl/audit?n` | 人工操作、拒绝、锁冲突审计 |
| GET | `/_ctl/stop-points` | 当前模式已装载的停点清单 |
| POST | `/_ctl/<op>/request` | → `{op, challenge_id, expires_at, expires_in_s, summary, confirm_endpoint, cancel_endpoint}` |
| POST | `/_ctl/<op>/confirm` | 体 `{challenge_id, ...}`；执行并返回 `{performed:true, ..., status}` |
| POST | `/_ctl/<op>/cancel` | 体 `{challenge_id}`；`{cancelled:true, state_changed:false}` |

`<op>`：`select_mode`（`{mode:"A"|"B"}`）、`init_start`、`pause`、`resume`、`stop`、`confirm_stop_point`（`{stop_point_id, run_id, confirm_seq}` 三者必填）、`clear_unknown`（`{note?}`）。

错误一律 `409`（参数非法 `400`、未知操作 `404`），体为 `{code, message, detail}`。代码：
`E_STATE_NOT_ALLOWED`、`E_UNKNOWN_RESULTS_PENDING`、`E_INFLIGHT_PENDING`、`E_MODE_NOT_B`、`E_NOTHING_TO_CLEAR`、
`E_CHALLENGE_UNKNOWN|USED|EXPIRED|OP_MISMATCH|STALE`、`E_RUN_LOCK_BUSY`、
`E_STOP_POINT_MISMATCH`、`E_RUN_ID_MISMATCH`、`E_CONFIRM_SEQ_NOT_CURRENT`、`E_BAD_PARAM`、
`E_APPROVED_TARGET_NOT_CONFIGURED`、`E_TARGET_MISMATCH`、`E_DATA_DOMAIN_MISMATCH`、`E_ARTIFACT_MISMATCH`、`E_TARGET_NOT_READY`、`E_READYZ_*`、
`E_EVENT_COUNT`、`E_STOP_POINT_COUNT`、`E_SOUNDPROOF_COUNT`、`E_KNOWN_REJECT_IDS`、`E_SCENARIO_NOT_FOUND`、`E_B_SEGMENTS_MISSING`、`E_B_SEGMENT_SEQUENCE`、`E_MANIFEST_*`、`E_STEP_ORDER`。

## 3. 单轮状态与转换

| 显示状态 | 代码 | 进入 | 允许的人工操作 |
|---|---|---|---|
| 待启动 | `IDLE` | 初始状态 | `select_mode`、`init_start` |
| 初始化中 | `INITIALIZING` | `init_start` 的核对全部通过并已分配 `run_id` | `stop` |
| 运行中 | `RUNNING` | 初始化完成；或暂停后继续；或停点确认后继续 | `pause`、`stop` |
| 等待阶段确认 | `AWAITING_CONFIRM` | B 模式到达第 k 停点（含第 62 个） | `confirm_stop_point`（当前停点）、`stop` |
| 已暂停 | `PAUSED` | 运行中人工暂停 | `resume`、`stop` |
| 停止处理中 | `STOPPING` | 停止被接受且仍需确认在途结果 | （无；自动收敛） |
| 已停止 | `STOPPED` | 停止完成且无未知结果 | `select_mode`、`init_start` |
| 发送计划完成 | `COMPLETED` | 末项已发、全部结果有归属、B 62 次确认齐全 | `select_mode`、`init_start` |
| 异常停止 | `ABORTED` | 未知结果锁存／初始化不完整／运行器异常／重启发现活动轮次 | `clear_unknown`（有未核清项时）、`select_mode`、`init_start`（核清后） |

守卫表的完整断言见 `tests/control/test_machine.py`（含每个状态的否定用例）。

## 4. 与 V1.1 03 号 / E02 的逐条对照

| 设计条目 | 实现 | 验证 |
|---|---|---|
| §1 模式选择仅 A/B，运行前选定，运行中不得改 | `transition_error(select_mode)` 仅在 `IDLE/COMPLETED/STOPPED/ABORTED` 且无未核清项时放行；切换只写 `selected_mode.json` | `test_machine.py::test_mode_switch_forbidden_while_a_round_is_live`、`test_rounds.py::test_switching_mode_only_changes_the_selection` |
| §1 一次点击完成初始化并开始，无第二个开始按钮 | 唯一写状态的启动入口 `init_start`；页面只有一个 `btn-init`、只有一处 `op("init_start")` | `test_api.py::test_the_page_offers_exactly_one_start_button_and_a_confirm_dialog_per_action` |
| §1 B 阶段确认：显示阶段编号与停止位置；最后停点也确认；普通继续不能代替；旧/重复/错阶段确认不放行 | 停点携带 `sequence/total/label/stop_evidence/business_interval/events_completed`；确认需 `stop_point_id+run_id+confirm_seq`（严格等于当前停点且大于上次）；`resume` 在 `AWAITING_CONFIRM` 一律 409；确认码绑定状态指纹 | `test_rounds.py::test_mode_b_requires_every_stop_point_including_the_last`、`::test_wrong_old_and_duplicate_stop_point_confirmations_never_pass` |
| §1 暂停/继续沿原剩余偏移，不追赶等待时间 | 计划时钟扣除暂停时长与停点等待时长 | `test_rounds.py::test_pause_keeps_the_position_and_does_not_catch_up_the_waiting_time` |
| §1 停止本轮：不撤回已发、不清空、不自动重播 | `stop` 仅停止后续调度；无任何撤回/清理调用 | `test_rounds.py::test_stop_from_running_is_accepted_and_leaves_the_rest_unsent` |
| §1 再次初始化启动的前置（已完成或已停止、无活动任务、在途与未知已核清；暂停或 B 等待不满足） | 守卫表 + `unknown_pending`/`inflight` 条件 + 全局运行锁 | `test_machine.py::test_init_start_*`、`test_rounds.py::test_b_waiting_for_confirmation_cannot_be_re_initialised` |
| §2 重复点击不得创建第二个任务 | 状态守卫 + 运行锁 + 确认码一次性 | `test_rounds.py::test_double_click_never_creates_a_second_round` |
| §2 刷新/重连/进程恢复不得自动开始第二轮；位置不明保持停止并提示核查 | `recovered_state()`：活动状态一律恢复为 `ABORTED` + `requires_manual_review`；在途请求追加未知锁存 | `test_machine.py::test_restart_never_resumes_a_live_round`、`test_rounds.py::test_a_restart_never_resumes_a_round` |
| §3 数据集与场景核对、进度归零、B 清除上一轮确认、处理旧暂停/未完/在途后才接受 | 启动前 `load_plan()` 全量核对；`run_id` 分配后重置 `progress/confirm_seq/confirmed_stop_points/counts/stop_point` | `test_plan.py` 全部、`test_rounds.py::test_a_plan_that_does_not_match_the_frozen_counts_refuses_the_start` |
| §3 原事件 ID／时间／身份不变 | 控制层只读剧本步骤，发送前后不改业务字段；窄拒绝判据再次比对计划原值 | `test_outcome.py::test_changed_business_values_are_unknown`、`test_rounds.py::test_formal_mode_a_round_sends_318_events_in_plan_order` |
| §4 原 10 条照原时刻顺序发送、不预检阻止 | 计划不过滤任何事件；`SOUNDPROOF` 身份集由剧本推导并核对为 10 条 | `test_rounds.py::test_known_type_rejection_is_recorded_and_the_round_continues`、`::test_formal_mode_b_round_has_62_stop_points_and_keeps_the_ten` |
| §4 已知类型拒绝的合取判据 | `outcome.classify()`：422 + 完整未截断正文 + 正文恰为 `{"detail":[...]}` 且每项 `loc==["body","event_type"]` + 身份属原 10 条 + 类型/业务值未变 + 目标为获准目标 | `test_outcome.py`（逐条否定用例） |
| §4 其他 422／认证失败／错目标／限流／500／超时／不可解析 → 停止推进、保留实际结果、不盲目重发 | `UNKNOWN` 锁存 → `ABORTED`；`send_once()` 单次发送，控制层无重试路径 | `test_rounds.py::test_an_undetermined_result_latches_the_round`（9 种故障）、`::test_the_control_layer_never_resends` |
| §4 `CV_MAX_RETRIES=0` 的既有风险控制保持 | 控制层只用 `send_once()`（恒一次尝试），不读取 `CV_MAX_RETRIES`；旧 `send()` 路径与默认值未改动 | `cv/pusher.py::send_once`；`test_rounds.py::test_the_control_layer_never_resends`（替身仅收到 1 次） |
| §5 发送计划完成 = 末项执行且结果有归属、B 62 次确认齐全；含已知拒绝须明示；提前停止/未知不显示完成 | `_finish_completed()` 的四项前置，不成立则 `ABORTED`；`completed_with_known_rejections` | `test_rounds.py::test_known_type_rejection_is_recorded_and_the_round_continues` |
| §5 不等 TRACKING_END、不清库、不改业务身份 | 代码中无任何平台查询、数据库访问或清理调用 | `test_api.py::test_status_reports_only_the_permitted_technical_state` |
| §6 仅最小技术状态；日志只排错，不生成报告/截图/工单/判定 | `status()` 字段固定；无报告/截图/工单产物 | 同上 |
| E02 全入口互斥 | 单一 `RunLock`（flock + 进程内锁）由 `/_ctl` 轮次、原生 scenario、`emit(-raw)` 共享 | `test_runlock.py`、`test_api.py::test_the_native_sending_entries_share_the_control_run_lock`、`::test_a_native_scenario_blocks_the_manual_round_and_releases_the_lock` |
| E02 目标身份校验 | `verify_target()`：scheme+host+port+path 逐段比对 + `/readyz` 的 `data_domain`/`artifact`/`ready` | `test_target.py`、`test_rounds.py::test_a_target_that_is_not_the_approved_one_refuses_the_start` |
| E02 二次确认两段请求 | `request`/`confirm`/`cancel`，60 s、一次性、绑定操作与状态指纹 | `test_challenge.py`、`test_api.py::test_two_phase_confirmation_over_http` |

## 5. 偏差与工程判断（需 Owner/设计方知悉）

1. **窄拒绝只接受 HTTP 422**。E02 写「HTTP 4xx + 合同枚举」，V1.1 §4 与 F11 明确把 400/500 等列为未知。实现取更窄者：仅 422 可进入已知拒绝判据，400/403/409 等一律 `UNKNOWN`。
2. **新增 `clear_unknown` 操作**（第 7 个操作，同样两段确认与留痕）。V1.1 §1 把「在途与未知结果已由技术人员核清」设为再次启动的前置，但未给出该「核清」的操作载体；没有它，一次未知结果会使模块永久无法再启动。它只把 `unknown_pending` 移入 `unknown_cleared`，并向 `unknown.jsonl` 追加 `CLEARED_BY_TECHNICIAN` 记录——**不删除任何实际技术结果**。
3. **核对与初始化分两步**。为满足「目标不一致不消耗 run_id」，剧本装载与目标校验在分配 `run_id` 之前同步执行；失败时状态不变、不进入「初始化中」、不发送任何事件，仅写审计。`INITIALIZING` 因此只覆盖「准备」（进度归零、清除上一轮确认状态）。
4. **B 停点来源**。停点即剧本包预切的 `<ds>_B060_pNN` 场景本身（62 段），停点依据标签取自可选的 `manifest.json`（= 剧本包 `cv/manifests/<ds>.json`）。装载时交叉核对 62 段事件序列与连续剧本 `_B060`/`_A240` 完全一致，并核对 manifest 段数/段名/事件数；`manifest.json` 缺失时仍可运行，但停点标签退化为段名——**正式运行应随数据一起装载 manifest**。
5. **`manifest.json` 的放置**。现有 `Dataset` 只读取 `<data_root>/<dataset>/<name>.json`，而剧本包把 manifest 放在 `cv/manifests/<ds>.json`。装载（IS-04）需把它复制为 `cv/data/<ds>/manifest.json`。本分支不装载正式数据。
6. **原 10 条身份集的来源**。默认由剧本推导（`event_type == SOUNDPROOF_COVER_MOVE_COMPLETED`）并强制为 10 条；可用 `SIM_CONTROL_KNOWN_REJECT_EVENT_IDS` 固定显式清单，不一致即拒绝启动。代码内不内嵌业务 ID。
7. **`/_ctl` 的来源白名单**。`simcore/ipguard.py` 原仅覆盖 `/_sim/*`；已把 `/_ctl*` 纳入同一管理端白名单，否则新页面会落入「合同路径默认开放」分支。这是对既有 F02 纠偏范围的必要扩展。
8. **状态页鉴权**：页面 HTML 本身不带密钥、不含状态，只受来源白名单限制；所有数据与动作端点仍需管理令牌（浏览器首次访问时由页面提示输入并存于 `sessionStorage`）。
9. **`CV_MAX_RETRIES` 默认值未改**（仍为 3，供旧 `send()`/scenario 路径使用）。控制层不读该值，交接时仍须按既有风险控制在正式环境核实 `CV_MAX_RETRIES=0`。

## 6. IoT reload/dataset 副作用的实际核实

V1.1 §3 要求核实「现有 IoT reload/dataset 可能重建历史、覆盖本地发送配置并重置脚本游标」。源码核实结果（`iot/app.py`、`iot/store.py`、`iot/publisher.py`）：

| 调用 | 实际副作用 | 结论 |
|---|---|---|
| `POST /_sim/reload`、`POST /_sim/dataset`（IoT 进程） | `on_dataset_change=_reload_store` → `store.load_from_dataset(ds, keep_runtime_config=False)`：清空并按数据集重建全部历史（`history.clear()`），**用数据集 `send_config.json` 覆盖运行时发送配置并落盘**（忽略 `IOT_KEEP_RUNTIME_CONFIG`），`config_version += 1`；随后 `publisher.reset_script()` 把脚本游标与起点归零 | 确有三项破坏性副作用；进程启动时的 `load_from_dataset(keep_runtime_config=True)` 反而会保留已落盘的运行时配置 |
| `POST /_sim/mqtt/script/reset`、`/_sim/mqtt/pause|resume`、`/_sim/mqtt/publish` | 重置脚本游标 / 暂停发布 / 追加一条历史样本 | 均为发布侧状态变更 |

**本控制模块采取的顺序：对 IoT 不做任何调用。** 理由：

- 本模块的「初始化」按 V1.1 §3 只作用于**本轮要推进的发送计划**（CV 正式推送）及其进度与确认状态；IoT 在本剧本中是「按目录/历史请求应答」的查询服务，33 个窗口不由按钮请求，MQTT 变化流（O05）未准入、保持 `IOT_MQTT_ENABLED=false`。
- IoT 与 CV 是**两个独立进程**（`sim-iot` / `sim-cv`），控制模块只在 CV 进程内；跨进程调用 `/_sim/reload` 既会丢掉测试人员已做的 IF-IOT-005 全量更新结果，又会清空历史，等于「为按钮无理由重启已运行服务」（§3 明确禁止）。
- 因此装载/重载 IoT 数据集是**人工装载步骤（IS-04/T04）**的职责，须在本轮启动**之前**完成，且必须满足 `IOT_HISTORY_CAP_PER_PARAM>=30257`（默认 5000 会丢 198,404 行）与 `IOT_MQTT_ENABLED=false`。模块启动时不校验 IoT（跨进程、无权限），该前置由运行手册承担。

## 7. 未决项

1. `sent_at` 更新与接收端幂等的兼容性仍未决（V1.1 §4）：`send_once()` 每次尝试刷新 `sent_at`，本轮不新增重试语义，也不宣称问题已解决。
2. 目标 `/readyz` 的字段形态取自母本 `docs/wave6/F19/existing-runtime/readiness.json`（顶层 `artifact`/`data_domain`/`ready`），并兼容嵌套 `data_domain.observed.data_domain`；新实例的真实响应须在接入时实测核对。
3. 真实接收端对原 10 条的**实际**响应未核实（本轮不接真实目标）：若实际接受，按实际记录接受，不预设 10 次拒绝。
4. 页面未做人员身份区分（仅管理令牌 + 来源白名单）；若需区分操作者身份，须由部署侧前置鉴权提供。
5. B 模式段落推进使用 `manifest` 的 `nominal_seconds`（缺失时退化为段内末事件偏移）作为段起点累加基准；与现场对「段间名义时长」的期望须在联调时核对。
