# interface-simulators-5w — 新五墙 A/B 集成测试环境部署说明

U01 V1.2 D-7 第 4 步成果。本目录是**新五墙实例**的全部部署配置；旧实例
（项目 `interface-simulators`、容器 `sim-*`、端口 18001-18004/18830、网络
`interface-simulators-net`）完全不受本目录影响，也不得被本目录的任何命令触碰。

## 1. 对象清单（白名单，仅此 11 项）

| 类别 | 对象 |
|---|---|
| Compose 项目 | `interface-simulators-5w` |
| 长驻容器 ×5 | `sim5w-mom`、`sim5w-iot`、`sim5w-esb`、`sim5w-cv`、`sim5w-mqtt` |
| 网络 | `interface-simulators-5w-net` |
| 镜像 | `interface-simulators-5w:b2c8d70`（E02_SIM 已构建并通过 103 项隔离测试，未重建） |
| 宿主端口 | `185.189.48.12:18011/18012/18013/18014`、`127.0.0.1:18831`、`127.0.0.1:18024` |
| 状态目录 | `./state-5w/`（`control/{A,B}` + `control/run.lock` + `iot/`） |
| 数据目录 | `./data-5w/`（`sims/` 只读挂载进容器，`reference/` 仅供人工取用） |

端口语义：

| 入口 | 用途 | 来源限制 |
|---|---|---|
| `http://185.189.48.12:18011` | MOM IF-MOM-001/002 合同口 | 合同路径开放；`/_sim/*` 仅回环+RFC1918 |
| `http://185.189.48.12:18012` | IoT IF-IOT-001..005 合同口 | 同上 |
| `http://185.189.48.12:18013` | ESB IF-SAP-001 / IF-WINDCHILL-001 合同口 | 同上 |
| `http://185.189.48.12:18014` | CV IF-CV-001 合同口 + `/_sim/health` | 同上（`/_ctl` 公网 403） |
| `127.0.0.1:18024` | **CV 的 `/_ctl` 人工 A/B 控制页受控本地入口** | 只绑回环，经 SSH 隧道访问 |
| `127.0.0.1:18831` | MQTT broker（O05 未准入，仅回环） | 只绑回环 |

控制页访问方式（测试人员，Owner §二.5「受控本地端口 + SSH 隧道」）：

```bash
ssh -N -L 18024:127.0.0.1:18024 <user>@185.189.48.12
# 浏览器打开 http://127.0.0.1:18024/_ctl/ ，首次使用输入管理令牌（SIM_ADMIN_TOKEN）
```

## 2. 文件

| 文件 | 说明 |
|---|---|
| `compose.5w.override.yaml` | 叠层：项目名/容器名/端口/镜像/卷/网络。**`!override`、`!reset` 必须保留**——compose 默认对 `ports`/`volumes`/`env_file` 是追加语义，不覆盖就会把旧端口 18001-18004/18830 一起发布并与运行中的旧实例冲突 |
| `compose-5w.sh` | 唯一入口，含工作树守卫（只允许 `/root/project/portal-5w/interface-simulators` + `5w/*` 分支）与 `down -v` 二次确认守卫 |
| `.env.5w.example` | 配置模板（占位值） |
| `.env.5w` | **真值，0600，已 gitignore，不入提交** |
| `secrets/mosquitto-passwd` | 新实例独立 MQTT 口令文件（0600，uid 1883），已 gitignore |
| `README.md` | 本文件 |

## 3. 首次部署步骤（已执行，此处留作可重复程序）

```bash
cd /root/project/portal-5w/interface-simulators
# 1) 配置：复制模板并填真值（新生成 SIM_ADMIN_TOKEN 与 MOM/ESB/MQTT 口令，不复用旧实例）
cp deploy/5w/.env.5w.example deploy/5w/.env.5w && chmod 600 deploy/5w/.env.5w && vi deploy/5w/.env.5w
# 2) MQTT 口令文件（容器内 uid 1883 读）
mkdir -p deploy/5w/secrets && : > deploy/5w/secrets/mosquitto-passwd
set -a; . ./deploy/5w/.env.5w; set +a
docker run --rm -v "$PWD/deploy/5w/secrets:/m" eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b /m/mosquitto-passwd '$IOT_MQTT_PUB_USER' '$IOT_MQTT_PUB_PASS' && \
   mosquitto_passwd -b /m/mosquitto-passwd '$IOT_MQTT_SUB_USER' '$IOT_MQTT_SUB_PASS'"
chown 1883:1883 deploy/5w/secrets/mosquitto-passwd && chmod 600 deploy/5w/secrets/mosquitto-passwd
# 3) 正式数据装载（见 §4）
# 4) 渲染核对 → 启动
deploy/5w/compose-5w.sh config | less      # 核对：容器名仅 sim5w-*、端口仅白名单、无 :local
deploy/5w/compose-5w.sh up -d
deploy/5w/compose-5w.sh ps
```

## 4. 正式数据装载方式（V1.1/T04）

**机制：只读绑定挂载，不烘进镜像**（镜像保持 E02_SIM 自测通过的 `b2c8d70` 不变，数据可独立核对与替换）。

```
./data-5w/sims/mom/<dataset>/orders.json                          -> 容器 /data-5w/mom   (MOM_DATA_ROOT)
./data-5w/sims/iot/<dataset>/{devices,params,history,realtime,send_config}.json
                                                                   -> 容器 /data-5w/iot   (IOT_DATA_ROOT)
./data-5w/sims/cv/<dataset>/{catalogue,scenarios,manifest}.json    -> 容器 /data-5w/cv    (CV_DATA_ROOT)
./data-5w/sims/esb/<6 组文档数据集 + sww_esb_formal_protocol>/      -> 容器 /data-5w/esb   (ESB_DATA_ROOT)
./data-5w/reference/…                                              -> 不挂载，供人工取请求体/预期/样例文件
```

- 数据来源：只从官方 ZIP 重新解压到空目录
  `/root/exports/T01_Engineering_Input_Baseline/02_Formal_Scenario/侧墙实际生产全过程动态回放与多系统集成测试剧本_V1.1_模拟器人工一键启动机制定点修订成果包.zip`，
  装载前与 `T04_file_inventory.csv` 逐文件核对 sha256+bytes：**771/771 一致**。
- `<dataset>` 六个：`sww_02_13_r`、`sww_03_05_l`、`sww_03_07_r`、`sww_03_08_r`、`sww_03_08_l`、`sww_all5`；
  运行默认 `SIM_DATASET=sww_all5`（全五墙），ESB 默认 `ESB_DATASET=esb_docs_normal`。
- **manifest 必须复制为 `cv/data/<ds>/manifest.json`**（E02_SIM Deviation 4）：`Dataset` 只读
  `<data_root>/<dataset>/<name>.json`，B 停点标签与 `nominal_seconds` 由它提供。已对六个数据集全部复制。
- **IoT 只在首次启动前装载，运行后不得 reload**：`POST /_sim/reload` / `/_sim/dataset` 会
  `history.clear()` 重建、无视 `IOT_KEEP_RUNTIME_CONFIG` 覆盖运行时发送配置并重置脚本游标，
  等于丢掉测试人员已做的 IF-IOT-005 结果（E02_SIM §5）。切墙必须按「停→改 `SIM_DATASET`→起」。
- `IOT_HISTORY_CAP_PER_PARAM=40000`（> 单参数最大 30257；默认 5000 会丢 198404 行）。
- `IOT_MQTT_ENABLED=false`、`CV_MAX_RETRIES=0`。
- ESB 文件下载基址：用剧本包官方工具 `esb/04_simulator/file_service/configure_download_base.py`
  把六组数据集的 Windchill 下载链接从 `http://127.0.0.1:8765` 改写为
  **`http://185.189.48.12:18013/esb-doc-test`**（新实例），41 条链接全部改写，`sap_docs.json` 字节未变，
  改写报告在 `./data-5w/sims/esb/deployment_mapping.json`。
  ⚠ **静态文件服务本身尚未部署**（本步白名单内没有文件服务对象）：链接指向新实例，但
  `02_sample_files` 的 8 份有效样例+1 份负例目前只在 `./data-5w/reference/esb/02_sample_files/`，
  需后续按 `file_service/nginx-esb-docs.conf.example` 由文件服务责任人发布后才可实际下载。

## 5. 本步后的状态与边界

- `/_ctl/status` = **IDLE**（A、B 两模式均 IDLE），`state-5w/control/{A,B}` 只有初始 `state.json`，
  **无 `results.jsonl`、无 `unknown.jsonl`、无 `audit.jsonl`** → 零操作、零发送。
- 正式 A/B 业务回放只能由**测试人员**经 `127.0.0.1:18024/_ctl/` 人工点击 → 二次确认启动；
  Owner 明确不授权由 Claude Code 代替人工执行。
- `CV_TARGET_BASIC_USER` / `CV_TARGET_BASIC_PASS` **留空**：**待人工 S01 来源绑定后填入**。
  本步不推送，留空不影响 IDLE 与健康；正式回放前必须由测试人员按新 YQYD S01 来源凭据填入并
  `deploy/5w/compose-5w.sh up -d`（`restart` 不会重新读取 env）。
- 单副本单卷：`/_ctl` 的 flock 只在共享同一 `SIM_CONTROL_STATE_DIR` 时有效，
  **CV 模拟器不得扩成多副本或多卷**（E02_SIM §7.8）。
- 本步未把任何服务接入 `yqyd-5w-pull` 网络（该网络属步 3 的 YQYD 对象，本步白名单内没有它）；
  若 YQ-06 需要用 socat 按名解析 `sim5w-*`，在步 3/5 按授权追加。

## 6. 回退（只删本步新建对象）

```bash
SIM5W_CONFIRM_DOWN_V=yes deploy/5w/compose-5w.sh down -v   # 只作用于 interface-simulators-5w
rm -rf state-5w                                            # 可选：清状态目录
```

旧项目**不得** `down`。
