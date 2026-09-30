# 实验室测量可信性追溯服务

基于 **Python 3.11 + FastAPI + SQLite** 的校准证书 / 测量读数可信性追溯 HTTP 服务。

- 证书、读数的每一次成功变更都产生**全局单调递增版本**；
- 证书历史**仅追加（append-only）**：只能撤销或由新证书纠正，更正使旧证书在新版本立即失效，历史不可覆盖；
- 读数结论按「测量时间 + 指定版本生效中的证书」实时计算：**未验证 / 待复核 / 合格 / 超限**；
- 撤销与纠正在新版本立即改变受影响读数的当前结论，而**旧版本结论及其所依证书仍可原样查询**；
- 所有写入必须携带**请求标识（request_id）**：相同标识 + 相同内容安全重试不产生新版本，标识复用但内容不同、无效区间、未知目标一律报错，且**不留任何部分变更**。

## 一、快速启动

### 方式 A：Docker Compose（一键启动并初始化数据库，推荐）

```bash
docker compose up --build -d
docker compose logs -f trace-service      # 查看日志
curl http://localhost:8000/healthz        # 健康检查
```

服务启动时自动执行数据库初始化（建表幂等）。数据保存在 Docker 命名卷 `trace-data` 中。

停止 / 清理：

```bash
docker compose down              # 停止，保留数据
docker compose down -v           # 停止并删除数据卷（清空数据）
```

### 方式 B：本地 Python 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.db init            # 初始化数据库（生成 data/trace.db，幂等）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| 服务根路径（跳转） | http://localhost:8000/ |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/healthz |

**监听地址/端口**：默认 `0.0.0.0:8000`。Compose 通过端口映射 `8000:8000` 暴露到宿主机，修改 `docker-compose.yml` 的 `ports`（如 `"18000:8000"`）即可更换宿主机端口。

## 二、配置

仅一个环境变量，均有默认值：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACE_DB_PATH` | `data/trace.db`（本地）；`/data/trace.db`（镜像） | SQLite 数据库文件路径。父目录不存在会自动创建 |

本地运行示例：

```bash
TRACE_DB_PATH=/var/lib/trace/lab.db uvicorn app.main:app --port 8000
```

## 三、领域规则

### 时间与区间

- 所有时间为 **UTC**，必须带时区偏移（用 `Z` 或 `+00:00`），否则返回 422；
- 证书有效期为 **左闭右开**：`valid_from_utc <= 测量时间 < valid_to_utc`；
- 允许值范围为**闭区间**：`value_min <= 数值 <= value_max`；
- 要求 `valid_from_utc < valid_to_utc`、`value_min <= value_max`，否则 422 且不产生版本。

### 结论判定（在指定版本 v 下）

1. 取该版本生效中的证书行：`version_valid_from <= v < version_valid_to`，且设备编号相同；
2. 再按测量时间落入证书 UTC 左闭右开有效期筛选匹配证书；
3. 结论：

| 匹配数 | 结论码 | 结论 | 含义 |
| --- | --- | --- | --- |
| 0 | 0 | `UNVERIFIED` 未验证 | 无有效证书覆盖测量时间 |
| 1 | 1 / 2 | `PASS` 合格 / `FAIL` 超限 | 唯一匹配，数值在闭区间内合格，否则超限 |
| >1 | 3 | `REVIEW` 待复核 | 多份有效证书同时覆盖测量时间 |

### 版本与历史

- 证书提交、纠正、撤销与读数提交，每次成功各占一个全局版本号（1 起递增）；
- 纠正：旧证书行状态置为 `SUPERSEDED`，其行版本区间在新版本闭合（左闭右开），并追加新行；
- 撤销：当前行状态置为 `REVOKED`，同样在新版本闭合；撤销/纠正后的编号不能以「提交」方式复活；
- 读数一旦写入永不修改；结论不物化，`结论 = f(读数, 版本)`，因此任意历史版本的结论都可重算；
- 写操作在单事务内完成：先做全部校验，再追加版本与数据；任何失败整体回滚。

### 请求标识与幂等

- 写入接口须提供请求标识：请求头 `X-Request-ID: <标识>` **或** JSON 字段 `"request_id"`（两者同时出现必须一致）；
- 相同标识 + 相同内容重复提交：返回首次响应（`replayed: true`），**不产生新版本**；
- 相同标识 + 不同内容：`409 REQUEST_ID_CONFLICT`；
- 无效区间/缺时区/NaN/Infinity：`422`；缺请求标识：`400`；未知证书或读数：`404`。

## 四、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/certificates` | 提交校准证书（201） |
| POST | `/api/v1/certificates/{cert_no}/correct` | 纠正证书（旧版本失效） |
| POST | `/api/v1/certificates/{cert_no}/revoke` | 撤销证书（body 可空） |
| POST | `/api/v1/readings` | 提交读数（201） |
| GET | `/api/v1/versions` | 版本流水（分页 `limit/offset`） |
| GET | `/api/v1/certificates?device_id=` | 证书全部历史行 |
| GET | `/api/v1/certificates/{cert_no}?at_version=` | 某版本所见证书 |
| GET | `/api/v1/readings?device_id=&at_version=` | 读数及其在指定版本的结论 |
| GET | `/api/v1/readings/{reading_no}?at_version=` | 单个读数指定版本结论 |
| GET | `/api/v1/readings/{reading_no}/timeline` | 读数结论随版本的变化时间线 |
| GET | `/api/v1/requests/{request_id}` | 请求标识幂等记录与首次响应 |
| GET | `/healthz` | 健康检查（返回最新版本号） |

## 五、调用示例

### 1. 提交校准证书

```bash
curl -X POST http://localhost:8000/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-cert-001' \
  -d '{
    "certificate": {
      "cert_no": "CERT-001",
      "device_id": "BAL-01",
      "valid_from_utc": "2026-03-01T00:00:00Z",
      "valid_to_utc":   "2026-04-01T00:00:00Z",
      "value_min": 10.0,
      "value_max": 20.0
    }
  }'
```

关键返回：

```json
{
  "action": "certificate.submit",
  "request_id": "req-cert-001",
  "new_version": 1,
  "certificate_state": "ACTIVE",
  "affected_readings_count": 0,
  "replayed": false
}
```

### 2. 提交读数（唯一匹配 → 合格/超限）

```bash
curl -X POST http://localhost:8000/api/v1/readings \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-reading-001' \
  -d '{
    "reading": {
      "reading_no": "RD-0001",
      "device_id": "BAL-01",
      "measured_at_utc": "2026-03-10T08:30:00Z",
      "value": 15.3
    }
  }'
```

```json
{
  "new_version": 2,
  "conclusion": {
    "conclusion_code": 1,
    "conclusion": "PASS",
    "explanation": "合格（唯一匹配，数值在闭区间允许范围内）",
    "matched_certificates": [ { "cert_no": "CERT-001", "...": "..." } ],
    "basis_certificate": {
      "cert_no": "CERT-001",
      "valid_from_utc": "2026-03-01T00:00:00.000000+00:00",
      "valid_to_utc": "2026-04-01T00:00:00.000000+00:00",
      "value_min": 10.0,
      "value_max": 20.0
    }
  }
}
```

多份证书有效期重叠且都覆盖测量时间时，`conclusion` 为 `REVIEW`，`matched_certificates` 列出全部候选。

### 3. 安全重试（同标识同内容，不产生新版本）

```bash
# 重复上一条读数请求：返回相同结果，new_version 不变，replayed=true
curl -X POST http://localhost:8000/api/v1/readings \
  -H 'Content-Type: application/json' -H 'X-Request-ID: req-reading-001' \
  -d '{ "reading": {"reading_no":"RD-0001","device_id":"BAL-01",
       "measured_at_utc":"2026-03-10T08:30:00Z","value":15.3} }'
# -> HTTP 201, "new_version": 2, "replayed": true
```

### 4. 纠正证书（旧证书新版本失效，受影响读数结论立即改变）

```bash
curl -X POST http://localhost:8000/api/v1/certificates/CERT-001/correct \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-cert-001-fix1' \
  -d '{
    "certificate": {
      "cert_no": "CERT-001",
      "device_id": "BAL-01",
      "valid_from_utc": "2026-03-01T00:00:00Z",
      "valid_to_utc":   "2026-04-01T00:00:00Z",
      "value_min": 10.0,
      "value_max": 12.0
    }
  }'
```

返回中 `affected_readings` 给出结论前后对比，例如：

```json
{
  "new_version": 3,
  "superseded_version_from": 1,
  "affected_readings": [
    { "reading_no": "RD-0001",
      "before": { "conclusion": "PASS" },
      "after":  { "conclusion": "FAIL" } }
  ]
}
```

> 纠正不能改变证书对应的设备编号（返回 422）；确需变更设备请撤销后使用新证书编号。

### 5. 撤销证书

```bash
curl -X POST http://localhost:8000/api/v1/certificates/CERT-001/revoke \
  -H 'X-Request-ID: req-cert-001-revoke'
# （也可发送 JSON：{"reason": "超差召回"}）
```

撤销后相关读数当前结论变为 `UNVERIFIED`。

### 6. 查询当前结论与历史版本结论

```bash
# 当前最新版本下的结论
curl http://localhost:8000/api/v1/readings/RD-0001

# 指定提交版本下的历史结论（撤销/纠正前的结论与所依证书原样可查）
curl 'http://localhost:8000/api/v1/readings/RD-0001?at_version=2'

# 结论随版本变化的完整时间线
curl http://localhost:8000/api/v1/readings/RD-0001/timeline

# 某版本所见证书（v1 时为 ACTIVE 的旧内容；最新版本为纠正后的新行）
curl 'http://localhost:8000/api/v1/certificates/CERT-001?at_version=1'

# 版本流水
curl 'http://localhost:8000/api/v1/versions?limit=50'

# 请求标识幂等记录（首次写入的完整响应）
curl http://localhost:8000/api/v1/requests/req-cert-001-fix1
```

### 7. 错误响应（均不产生版本）

```json
{ "error": { "code": "REQUEST_ID_CONFLICT", "message": "请求标识 ... 已用于不同内容，标识不可复用" } }
```

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 缺少请求标识 | 400 | `REQUEST_ID_REQUIRED` |
| 头/体请求标识不一致 | 409 | `REQUEST_ID_CONFLICT` |
| 标识复用但内容不同 | 409 | `REQUEST_ID_CONFLICT` |
| 有效期/取值区间非法、时间不带时区 | 422 | Pydantic 校验错误 |
| 证书编号已生效仍提交 | 409 | `CERT_ALREADY_ACTIVE` |
| 撤销/纠正未知或已失效证书 | 404 | `UNKNOWN_CERTIFICATE` |
| 读数编号重复 | 409 | `READING_ALREADY_EXISTS` |
| 查询未知读数 / 版本 / 请求标识 | 404 | `UNKNOWN_READING` / `VERSION_NOT_FOUND` / `UNKNOWN_REQUEST` |

## 六、测试

```bash
pip install pytest httpx
python -m pytest -q
```

测试覆盖：幂等与标识冲突、无效区间回滚、四种结论与开闭区间边界、多证书待复核、
纠正/撤销改变当前结论、旧版本结论与所依证书回溯、历史不可覆盖、未知目标报错无部分变更等。

## 七、项目结构

```
app/
  config.py    环境变量配置
  db.py        SQLite 连接、仅追加表结构、初始化（python -m app.db init）
  models.py    Pydantic 模型、UTC/区间校验、结论常量
  service.py   版本化写入、幂等事务、证书匹配与结论计算
  main.py      FastAPI 路由
tests/         端到端 API 测试
Dockerfile
docker-compose.yml
requirements.txt
```
