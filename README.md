# interface-simulators — MOM / IoT / ESB / CV 外部接口模拟器

高铁动车侧墙生产数字化项目第一阶段交付：四类外部生产系统接口的可运行模拟器，部署于 `185.189.48.12`。

工程目录：`/root/project/portal-20261022/interface-simulators/`（2026-10-27 起并入 portal-20261022 VS Code 工作区，与四个业务仓库并列）；任务卡原路径 `/root/project/interface-simulators/` 保留为指向该目录的符号链接。

| 目录 | 内容 |
| --- | --- |
| `mom/` | IF-MOM-001 获取订单列表、IF-MOM-002 请求订单下发（HTTP/JSON，Basic Auth） |
| `iot/` | IF-IOT-001～005（HTTP/JSON，X-Factory-Code）、IF-IOT-MQTT-001 发布器、Mosquitto 配置 |
| `esb/` | IF-SAP-001、IF-WINDCHILL-001（SOAP 1.1/XML，Basic Auth） |
| `cv/` | IF-CV-001 事件发送器（模拟机器视觉系统）+ 验证用接收端 |
| `simcore/` | 公共库：配置、数据集、管理端点、请求记录 |
| `simcore/control/` | 人工 A/B 单轮控制（IS-06/07/08）：单轮状态机、二次确认门、全入口互斥锁、窄拒绝判据、目标身份校验、最小状态页 |
| `tests/conformance/`、`tests/unit/` | 37 项符合性测试（预期取自接口定义原件）+ 5 项定点验证（F02/F04）+ 5 项单元测试 |
| `tests/control/` | 人工单轮控制的隔离技术测试（本地替身目标，`--network none`，不接真实目标） |
| `deploy/` | `init-env.sh` / `up.sh` / `restart.sh` / `down.sh` |
| `docs/` | 使用与配置说明、差异与未决事项、验证报告、最终执行报告、`evidence/` 运行证据 |
| `inputs/` | 接口定义/使用说明原件 ZIP（只读，SHA-256 已核对）及解包副本 |

快速开始：

```bash
./deploy/init-env.sh && ./deploy/up.sh      # 生成凭据并启动
./tests/conformance/run.sh                   # 对公网入口运行符合性测试
```

公网入口：MOM `:18001`、IoT HTTP `:18002`、ESB `:18003`、CV `:18004`、MQTT `:18830`。详见 `docs/接口使用与配置说明.md`。

Authority：`接口定义说明书-1021.zip`（优先）> `接口使用说明书-1021.zip`。未决项处理见 `docs/差异与未决事项说明.md`。

2026-10-27 定点纠偏（F02–F05）：管理端点 `/_sim/*` 默认仅允许本机/内网来源，CV `target_url` 受主机白名单限制，剧本 stop 等待线程退出；详见 `docs/定点纠偏执行报告-F02-F05.md`。

## 人工 A/B 单轮控制（分支 `5w/sim-manual-one-shot-20261030`）

依据 V1.1 正式设计 03《A/B 模拟器人工单轮运行设计》与 E02 三域设计说明 SIM 节，在 CV 模拟器进程内新增 `/_ctl/*`：

- `GET /_ctl/`：最小状态页（单 HTML，无框架）。模式选择、**一个**「初始化启动」按钮、暂停/继续/停止、B 当前停点确认；每个动作先弹「确认/取消」，取消不发任何请求。
- `GET /_ctl/status | /_ctl/results | /_ctl/unknown | /_ctl/audit | /_ctl/stop-points`：只读技术状态与留痕。
- `POST /_ctl/<op>/request` → `{challenge_id, expires_at, summary}`（60 s、一次性、绑定操作与状态指纹）→ `POST /_ctl/<op>/confirm {challenge_id}`；`POST /_ctl/<op>/cancel` 不改状态。
  `<op>` ∈ `select_mode | init_start | pause | resume | stop | confirm_stop_point | clear_unknown`。
- 全入口互斥：`/_ctl` 的一轮、`/_sim/cv/scenario/run`、`/_sim/cv/emit(-raw)` 共享同一个全局运行锁（文件锁 + 进程内锁），被占时一律 409 并写审计。
- 运行前校验 `CV_TARGET_URL` 与 `SIM_CONTROL_APPROVED_TARGET_URL` 逐段一致，并核对目标 `/readyz` 的 `data_domain` / `artifact`；不符拒绝启动且不分配轮次。
- `/_ctl/*` 与 `/_sim/*` 同样受 `SIM_ADMIN_TOKEN` 与 `SIM_ADMIN_ALLOW_CIDRS` 限制。

配置键见 `.env.example` 的「人工 A/B 单轮控制」节；逐条设计对照、偏差与未决项见 `docs/人工AB单轮控制设计对照.md`。
