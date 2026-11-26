# 馆校组合参访的到场互认与代付核销

每个 tick 是同一活动日的 15 分钟。成人与未成年人分别计名额。资助合同的站点条件见 sponsorship；纸凭条的 serial 由实际出具的馆方编码。本地登记均为合成数据。

## 资料端

`records/` 下的 `groups`、`venues`、`sponsorship` 来自不同业务经办方。文件里按该来源的登记顺序列出原始观察。字段 `id` 在本来源内唯一。报告收到的时刻与业务实际发生的时刻分别保留，查询不据此代替任何业务确认。

需要 Python 3.11.2，使用标准库。服务监听回环地址。

```sh
python3 -m unittest -v
python3 desk_source.py --port 8765
```

- `GET /sources` 返回可查询的来源名和各来源的记录数量。
- `GET /sources/来源名?offset=0&limit=20` 查询该来源的一页登记。读取下一页时用上一次返回的 `next_offset`，其为 null 时到达末尾。
- 不存在的来源返回 404。页长支持 1 至 50，非法页长或偏移返回 400。
- `--records` 可指定另一组登记。每个 JSON 文件使用记录数组，文件名是来源名。

## 业务后台

`visit_desk.py` 在只读资料端之上提供报名投影、两馆放行、拆队交接和核销。后台自己作出的每个决定都以事件追加进 JSONL 台账（默认 `ledger/visit_events.jsonl`，已在 `.gitignore` 中忽略），重启后重放恢复；`records/` 永不被写改。

```sh
python3 visit_desk.py --port 8766
# --records 指定来源目录，--ledger 指定台账文件
```

### 业务规则落点

- 学生名额与陪同名额分别报名；第二站按场次的 `students_per_adult` 校验陪同比例，成人不占学生活动席位。
- 第二站放行人必须已有第一站**已核实**到场，或持有馆方确认的直达例外（`direct_arrival_allowed` / `/direct-arrivals`）。直达放行与资助是否承认第一站互不影响：前者只看场馆门控，后者按资助合同的 `required_stations` 核销。
- 教师不能自行把缺席改成已参观：直达确认、纸凭条核实、拆队与跨校接待确认都要求 `"by": "venue"`，否则 403。
- 纸凭条默认待核实：未核实挂单返回 `pending_verification`，不会给第二站自动背书；核实结论只增不改，驳回纸凭条不影响已经查实的扫码器到场。同一人同一站的重复入场一律拒绝（纸凭条与扫码器之间也互相校验）。
- 拆队（`groups` 中的 `split_request`）和迟到学生跨校接待都要馆方重新确认目标时段的容量与陪同比例；已入场者钉在原实际场次，只能在馆交接不能改约。场次满员时看板给出同站有余量的场次和尚未入场、可改约的人。
- 入场记录带站点、场次与实际 tick，不在场次 `admit` 窗口内的登记被拒。
- 资助核销只认合同 `eligible` 名单内、完成全部 `required_stations` 的学生，并受 `student_limit` 限制；普通自费学生票、陪同票、代付名额是三个独立口径。学校已自费、其后改用代付名额完成参访的，票款差额退给付款学校；打包总额无法按人归属时显式标注，不做静默拆分。

### HTTP 接口

只读：

- `GET /people`、`GET /people/{person}`：每人在两站的归属场次、到场、直达例外与待核实纸凭条。
- `GET /sessions`、`GET /sessions/{session}/board`：名册、已入场、剩余活动席位、满员改约选项。
- `GET /sponsorship?grant=...` 或 `?school=...`：合同符合情况、可付人数与金额。
- `GET /finance/{school}`：三口径票款、代付核销与应退差额。
- `GET /reviews`：全部放行判定（含驳回原因与待决挂单）；`GET /ledger`：事件台账。

写入（均为 POST JSON，错误返回 `{error, message}`，403 为权限不足、404 为对象不存在、409 为业务规则冲突、400 为请求格式问题）：

- `POST /entries/review`：`{session, person, tick}` 现场登记，或 `{observation_id}` 挂接离线观察。
- `POST /paper/{observation_id}/verify`：`{verified, by}`，馆方核实纸凭条。
- `POST /direct-arrivals`：`{session, person, by}`，馆方确认第一站例外直达。
- `POST /splits/{request_id}/confirm`：`{by, tick?}`，确认团体拆班。
- `POST /host-transfers`：`{person, session, taking_adult, tick, by}`，确认迟到学生由另一学校随组接待。
