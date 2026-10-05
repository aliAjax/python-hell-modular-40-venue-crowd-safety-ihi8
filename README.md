# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 入场批次回传对账

场馆断网时入场口继续本地放行，网络恢复后把批次传回指挥中心对账。每个批次携带`gate_id`、`zone_id`、`count`和`batch_no`（批次号）。

- **幂等回传**：同一`batch_no`重复回传只算一次，人数不重复累加。
- **并发对账**：两个入场口同时提交同一区域的批次时，在单事务内按最新已提交占用重算；超出容量的人数照实计入，并在批次上标记`exceeded_capacity`/`excess_count`，区域置`over_capacity`，等指挥员复核。
- **状态变更作废**：区域状态一变（限流、疏散、关闭等），该区域所有`pending`批次作废为`void`；已回传批次不受影响。
- **整批重试**：回传失败后可整批重试，失败的回传不会创建批次、不会累加人数。
- **旧库回填**：升级旧库时按已回传批次`SUM(count)`重算各区域在场人数。

批次状态：`pending`（已登记待回传）→ `returned`（已回传对账）/ `void`（已作废）。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、批次对账（单事务原子回传、待回传批次作废、占用回填）和审计查询。
- `src/service.py`：用例编排、权限校验、版本控制、批次回传对账、复核和回填。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
# 升级旧库时按已回传批次回填区域在场人数
python3 app.py --db ./data.db --port 8340 --backfill
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`admission_batch`为入场批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`
- `POST /api/admission_batches`：登记待回传批次（`pending`，幂等）
- `POST /api/admission_batches/return`：回传批次并对账（幂等，超容量标记复核）
- `GET /api/admission_batches/review`：列出超容量待复核批次
- `POST /api/admission_batches/<id>/review`：指挥员复核超容量批次
- `POST /api/ops/backfill_occupancy`：按已回传批次回填区域在场人数

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
