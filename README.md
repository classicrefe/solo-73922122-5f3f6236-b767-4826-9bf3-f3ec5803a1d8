# 建设年报改造完工楼栋补算 API

按**现行区界**补算历年改造完工楼栋数的后端服务。给定各期旧区完工底账、新旧区
重叠范围的楼栋总数、各新区调档费用，在调档结果与底账相容的前提下，给出
**总费用最低的待调名单与金额**；照单调完后可**唯一**算出各新区完工数。

技术栈：FastAPI + SQLite（WAL）+ Pydantic，Docker Compose 启动。

## 1. 问题建模与关键结论

对每个期次：

- 旧区 `O` 有底账完工数 `c_O`；
- 新区 `N` 与旧区 `O` 的重叠范围有楼栋总数 `t_{N,O}`；
- 未知量 `x_{N,O}` = 现行新区 N 在原旧区 O 范围内的完工楼栋数，满足
  `0 ≤ x_{N,O} ≤ t_{N,O}`，且每个旧区 `Σ_N x_{N,O} = c_O`；
- 调一份新区 N 的档案，即得到该新区所有边上的 `x_{N,O}`，费用 `f_N`
  （正整数，每期最多 18 个新区）。

**何时能唯一确定全部未知量？**
底账相容时，只要每个旧区至多剩一条「未查」边即可：0 条未查边时余数必须为 0；
1 条时其值等于旧区完工余数。因此同一旧区下不允许同时存在两个未查新区——
这些新区两两构成**冲突**。待查集合必须覆盖冲突图的所有边，即冲突图的
**顶点覆盖**，目标是**费用最小的顶点覆盖**（等价于最大权独立集的补集）。
新区数 ≤ 18，服务用位掩码 DP 在 `O(2^k)`（实测 k=18 约 0.22 秒）内精确求解，
tie-break 规则：费用低 → 待调个数少 → 名单编码字典序小。

**底账相容性（登记校验，逐旧区独立）**

1. 各范围查回数介于 0 与该范围楼栋总数之间；
2. 已查总数 ≤ 旧区底账（完工余数非负）；
3. 完工余数 ≤ 未查楼栋总数（余数能由未查楼栋凑足）。

整数边容量 `0 ≤ x ≤ t` 下，条件 2、3 同时成立即保证存在可行分配。

## 2. 数据模型（SQLite）

| 表 | 关键字段 |
|---|---|
| `period` | 期次编码、名称 |
| `old_district` | 期次、编码、名称、底账完工数 |
| `new_district` | 期次、编码、名称、调档费用（>0） |
| `overlap` | 期次、新区、旧区、重叠楼栋总数（>0），三元组唯一 |
| `retrieval` | 期次、新区、旧区、查回完工数，`(期次,新区,旧区)` 唯一 |

并发与事务：

- 开启 **WAL**：核算走只读快照（`BEGIN`），登记走写事务，二者不互斥；
- 所有登记使用 **`BEGIN IMMEDIATE`** 立即取写锁，并发登记被数据库串行化，
  配合 `busy_timeout=5000` 等待锁；
- 任一校验失败即 `ROLLBACK`，**原记录保留**；
- 同一新区相同结果重送幂等返回 201，异值返回 409 且不覆盖；
- 计划/核算端点在单个只读事务内读同一已提交状态，核算期间登记照常进行。

## 3. API 一览（启动后见 `/docs` 交互文档）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/periods/` | 建立期次底账（旧区完工数、重叠楼栋总数、调档费用） |
| GET | `/periods/` | 期次列表 |
| GET | `/periods/{code}` | 期次底账详情 |
| GET | `/periods/{code}/plan` | **总费用最低的待调名单与金额**（只读快照） |
| GET | `/periods/{code}/accounting` | 核算；无需再调档时返回各新区完工数 |
| POST | `/periods/{code}/retrievals` | 登记一份新区档案（同值幂等、异值退回） |
| GET | `/periods/{code}/retrievals` | 已登记档案 |
| DELETE | `/periods/{code}/retrievals` | 清空该期登记（便于在 /docs 重复试用） |

## 4. 用 Docker Compose 启动

```bash
docker compose up --build
# 打开交互文档
# http://localhost:8000/docs
```

数据持久化在命名卷 `retrocalc-data`（容器内 `/data/app.db`）。
首次启动自动播种演示期次 `DEMO-2023`（库中已有期次时不重复播种）。

本地无 Docker 时的等价运行方式：

```bash
python3 -m venv --without-pip .venv          # 按环境引导 pip 后
.venv/bin/pip install -r requirements.txt
APP_DB_PATH=./data/app.db .venv/bin/uvicorn app.main:app --port 8000
```

## 5. /docs 试用脚本（调档前 → 调档后核算）

演示期底账：旧区 O1=30、O2=24、O3=18、O4=10；
新区费用 N1=12、N2=7、N3=3、N4=10。
冲突图的最大权独立集为 {N1, N4}（可省 22），故**最低费用待调名单 = {N2, N3}，金额 10**。

```bash
B=http://localhost:8000/periods/DEMO-2023

# 调档前：拿到最低费用名单
curl -s $B/plan
# status=retrieval_required, plan=["N2","N3"], total_fee=10
curl -s $B/accounting        # determined=false，尚不返回完工数

# 登记 N2 档案（查回数必须覆盖该新区涉及的全部旧区 O1/O3）
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":10,"O3":6}}'
# 名单收缩为 ["N3"]，金额 3

# 登记 N3 档案
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N3","counts":{"O2":14,"O3":2}}'
# status=completed，plan=[]

# 无需再调档，返回各新区完工数
curl -s $B/accounting
# N1=30, N2=16, N3=16, N4=20，合计 82 = 底账合计 30+24+18+10
```

校验类试用：

```bash
# 查回数超楼栋总数 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":16,"O3":0}}'
# 旧区集合不符 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":5}}'
# 同值重送 -> 201 幂等；异值重送 -> 409 原记录保留
# 完工余数无法由未查楼栋凑足 -> 409
```

## 6. 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

- `tests/test_api.py`：建档校验、最低费用计划、四类登记校验、幂等/异值、
  计划推进、唯一核算、**真实 uvicorn 下的并发登记**、快照读不阻塞写；
- `tests/test_solver.py`：18 新区完全图性能与正确性、链状图选便宜覆盖、
  脏数据触发 `infeasible` 防御、单未查边唯一求解。

## 7. 目录结构

```
app/
  db.py        # SQLite 连接、WAL、IMMEDIATE 写事务、只读快照
  schemas.py   # Pydantic 请求/响应模型
  services.py  # 相容性校验、最小权顶点覆盖 DP、唯一核算
  seed.py      # 演示期次种子数据
  main.py      # FastAPI 路由
tests/         # pytest 套件
Dockerfile  docker-compose.yml  requirements.txt  pytest.ini
```
