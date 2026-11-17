# 航运舱单拆并箱与重量守恒

给货代单证员使用。主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

七个业务模块：舱单采信、包装映射、拆并编组、危险品检查、净毛重核验、报关对账、版本封存。另有改单放行层：船开前对当前批提交拆单、并箱、换箱或更正申报变更单。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

访问 http://localhost:18241 。启动直接载入现场脏批。切换干净样例和旧编码样例可对照核验。干净批42个节点、6个主单，件数240、净重2400kg、毛重2640kg；现场脏批包含父级缺失、依赖环、危险品不相容及1kg毛重差异，封存状态为待处理。

各模块页面对应 `/<模块英文名>`，各模块执行入口为 `POST /api/<模块英文名>`，不传参即可独立演示。完整运行 `POST /api/runs` 支持 `dataset`（dirty/clean/legacy）、`csv_text` 与 `overrides` 参数。批次历史 `GET /api/runs`，详情 `GET /api/runs/{run_id}`。

同输入与规则版本生成相同批号，只保留一条批次。复核入口 `POST /api/runs/{run_id}/review` 提交当前 `revision`，陈旧提交409。归档包只有一个下载入口 `GET /api/export/{run_id}`，含证据和运行摘要。原始隔离在页面同屏显示。

## 改单放行

`/changes` 页提交变更单（JSON），支持 `split` 拆单、`merge` 并箱、`swap` 换箱、`redeclare` 更正申报。变更作用于当前批采信记录，候选舱单重新走全流程核验；隔离行原值随候选批保留，旧拆并关系被新关系整体替换，页面不再残留旧关系。

每张主提单给出当前关系、阻断原因（拆并链未守恒、危险品同箱说不清、申报重量脱节、链上隔离行）和修改前后差异。整批裁决按能否继续报关：全部通过且无批级阻断为整批放行；有主单通过且与阻断主单无同箱牵连为部分放行，只放行核对通过的主单；申报总量脱节、无通过主单或同箱牵连为整批待行。仅裁决通过的主单可下载封存件 `GET /api/changes/{change_id}/seal/{master_id}`，含当前关系、差异与判定证据。

整批结果统一生效：变更单落库与当前指针切换在同一事务，基准已被替换或切换失败时整体回滚，旧版保持当前，变更单以回滚状态留存修改前证据（409 `BASE_STALE`）。变更编号由变更单原文确定性生成，重跑同一变更只返回既有结果，不新增第二套当前结果。接口：`POST /api/changes`、`GET /api/changes`、`GET /api/changes/{change_id}`、`GET /api/current`。示例变更单见 `sample_data/change_fix.json`（修复）与 `sample_data/change_break.json`（违规）。

SQLite使用命名卷，容器重启后保留历史。要重建演示可在Compose中改用新的卷名称，保留旧卷。

源码在 `app/`，样例在 `sample_data/`，测试在 `tests/`；定义见 `docs/DATA-DICTIONARY.md`，模块和边界测试对应关系见 `docs/DEPTH.md`。输入上限2MB，属于离线复核工具。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
