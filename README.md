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
| `tests/conformance/`、`tests/unit/` | 37 项符合性测试（预期取自接口定义原件）+ 5 项定点验证（F02/F04）+ 5 项单元测试 |
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
