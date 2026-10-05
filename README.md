# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件、容量冲突和断网入场批次对账。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务，`admission_batch`为断网期间入场口本地放行、恢复后回传的入场批次。

## 入场批次对账

断网时入场口本地放行，网络恢复后通过`POST /api/admission_batches`回传批次，字段为`gate_id`、`zone_id`、`count`、`batch_no`、`recorded_at`、`zone_status`（入场口记录批次时看到的区域状态），可选`zone_generation`（区域状态代次，提供时参与作废判定）。

- 同一`(gate_id, batch_no)`重复回传只算一次：返回已存在批次和`deduplicated: true`，人数不重复累加，回传失败后可整批重试。
- 批次在单个写事务内按区域最新占用重算余量；多个入场口并发回传同一区域时互斥生效。超出容量的人数照实计入`current_occupancy`，批次置为`pending_review`并记录`over_capacity_by`，等待指挥员复核。
- 区域状态（或代次）与批次记录时不一致，批次置为`void`并作废，不计入在场人数。
- 指挥员（`coordinator`/`admin`）通过`POST /api/entities/<batch_id>/actions`，`action=review`、`data.reviewer_id`复核，`pending_review`变为`reviewed`。`GET /api/admission_batches?status=pending_review`可列出待复核批次。
- 批次状态：`applied`（已计入）、`pending_review`（超容待复核）、`reviewed`（已复核）、`void`（已作废）。
- 每次计入都会在区域的审计日志中写入`reconcile`条目，作为对账依据。
- 升级旧库时（`user_version`迁移）自动按已回传批次回填区域`current_occupancy`：`applied`、`pending_review`、`reviewed`批次的人数求和，`void`不计；无批次的区域保持原值。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/admission_batches`：回传入场批次（新建`201`，去重命中`200`）
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
