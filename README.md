# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切换核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。

## 唯一切换审计（single_open_cap）

关旧开新在同一事务内完成四步，缺一回滚：关旧写闸（`sessions.status open→closed` + 关闭审计 + 终态证明）→ 开新承接 → `visitor_open` 指针改挂 → 写不可变切换审计边 `session_switch_audits`（访客、旧会话、新会话、关闭审计键、时刻）。旧会话写闸由服务层 `ensure_session_writable()` 与库层触发器 `hits_reject_closed_session` 双重关闭，closed 会话不可追加。

只读核证接口：

| 接口 | 内容 |
| --- | --- |
| `GET /api/open-cap/proof` | open 唯一三重不变量 |
| `GET /api/open-cap/current` | 现行闸一览 |
| `GET /api/open-cap/audits` | 关闭审计一览 |
| `GET /api/open-cap/switches` | 切换审计边一览（可按 `visitor_id` 过滤） |
| `GET /api/open-cap/switch-proof` | 切换核证：逐边核访客/旧/新/写闸态/关闭审计键并给结论 |

