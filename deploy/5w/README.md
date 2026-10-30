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
| 镜像 | `interface-simulators-5w:<SIM5W_IMAGE_TAG>`（= 构建该镜像时的工作树 HEAD 短 sha；实际值在 `.env.5w`，并记录在证据目录；见 §7） |
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
  改写报告在 `./data-5w/sims/esb/deployment_mapping.json`。文件服务见 §4.1。

### 4.1 ESB 文档文件服务（`/esb-doc-test`）

IF-WINDCHILL-001 只返回下载链接，链接必须真的能取到文件。剧本包把这件事交给
「技术人员已有的测试服务器」（`file_service/nginx-esb-docs.conf.example`）；
这里用 **ESB 模拟器进程内的只读静态挂载**做最小等价实现，**不新增容器、不新增宿主端口**
（仍是 `sim5w-esb` 的 `185.189.48.12:18013`）。实现在 `esb/app.py` 的 `_mount_doc_files()`。

| 环境变量 | 值 | 作用 |
|---|---|---|
| `ESB_DOC_URL_PREFIX` | `/esb-doc-test` | URL 前缀（与改写后的链接一致） |
| `ESB_DOC_FILES_DIR` | `/esb-doc-files` | 服务根（挂 `./data-5w/esb-doc-files`，只读）。留空 => 全部 404 |
| `ESB_DOC_FILES_NEGATIVE_DIR` | `/esb-doc-files-negative` | 负例独立子树（挂 `./data-5w/esb-doc-files-negative`，只读）。留空 => 负例 404 |

预期行为（来源 `expected_responses/independent_expectations.json`，已实测，证据见
`T02_runtime/step-4/esb-doc-service/`）：

| 路径 | 预期 | 依据 |
|---|---|---|
| `/esb-doc-test/healthy/<8 份有效样例>` | **200** | 8 份 healthy 样例 |
| `/esb-doc-test/negative/ESBTEST_NEGATIVE_TRUNCATED.pdf` | **200**（能下载、打不开） | `INJECTED_UNREADABLE`，`expected_file` 指向该样例 |
| `/esb-doc-test/missing/ESBTEST_NOT_DEPLOYED.docx` | **404** | `INJECTED_404`，`package_file=null`，**任何时候都不得在此放文件** |

边界：只读（只 GET/HEAD，其它方法 405）、不列目录、路径穿越被拦截、只服务上面两个样例目录
（不服务工程源码/凭据/数据集）；响应头与交接的 nginx 配置一致
（`Content-Disposition: attachment`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`）；
`.docx`/`.xlsx` 的 MIME 已显式登记。该前缀是测试人员浏览器要取的合同类路径，
受 `SIM_CLIENT_ALLOW_CIDRS`（默认空=开放）约束，**不在**管理面白名单内。
按交接配置的告诫，负例放在独立子树、不与 `/healthy/` 混放，并由独立环境变量单独开关：
非约定的 ESB-11 故障轮次可把 `ESB_DOC_FILES_NEGATIVE_DIR` 留空再 `up -d esb`，该路径即回到 404。
实际状态可在 `/_sim/status` 的 `doc_file_service` 字段如实读到（含已服务文件清单）。

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

## 7. 重建镜像与单服务更新

镜像标签恒等于**构建时的工作树 HEAD 短 sha**，由 `.env.5w` 的 `SIM5W_IMAGE_TAG` 指定，
`compose-5w.sh` 会校验该变量存在且本机确有该镜像（否则拒绝执行，绝不静默跑错镜像）。

```bash
cd /root/project/portal-5w/interface-simulators
git commit ...                                   # 先提交代码改动
SHA=$(git rev-parse --short HEAD)
docker build -t interface-simulators-5w:$SHA .   # 构建（Dockerfile 只 COPY 代码，不含数据/凭据）
sed -i "s/^SIM5W_IMAGE_TAG=.*/SIM5W_IMAGE_TAG=$SHA/" deploy/5w/.env.5w
deploy/5w/compose-5w.sh up -d esb                # 只更新需要的服务
```

⚠ **不要在人工 A/B 单轮运行期间执行不带服务名的 `up -d`**：那会按新标签重建 `cv` 容器，
正在进行的轮次会被中断（进程重启后按设计一律落到「异常停止 + 需人工核查」，不会自动续跑）。
只改了 ESB 代码时，`up -d esb` 即可；`mom`/`iot`/`cv`/`mqtt` 继续跑旧标签镜像是安全的
（它们的代码在新旧镜像里逐字节相同），下一个自然维护窗口再对齐即可。

当前实际值与逐条实测结果见
`/root/exports/U01_185新五墙AB统一实施_20261029/T02_runtime/step-4/esb-doc-service/`。
