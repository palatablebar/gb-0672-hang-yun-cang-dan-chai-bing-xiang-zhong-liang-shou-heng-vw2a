# 深度登记 航运舱单拆并箱与重量守恒

规模：中型。使用者：货代单证员。核心规则：主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 舱单采信 | 执行并复核舱单采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 包装映射 | 执行并复核包装映射 | `POST /api/packaging` | `app/modules/packaging/rules.py` | `tests/test_packaging.py` |
| M3 拆并编组 | 执行并复核拆并编组 | `POST /api/grouping` | `app/modules/grouping/rules.py` | `tests/test_grouping.py` |
| M4 危险品检查 | 执行并复核危险品检查 | `POST /api/hazard` | `app/modules/hazard/rules.py` | `tests/test_hazard.py` |
| M5 净毛重核验 | 执行并复核净毛重核验 | `POST /api/weight` | `app/modules/weight/rules.py` | `tests/test_weight.py` |
| M6 报关对账 | 执行并复核报关对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 版本封存 | 执行并复核版本封存 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码回退 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_source` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_source` |
| TRAP-04 | 分段表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_segment_header` |
| TRAP-05 | 缺列隔离 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_isolation` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_conflict_all_isolated` |
| TRAP-18 | 空态三分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_three_empty_states` |
| TRAP-15 | 吨与包装换算 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_pallet_real` |
| TRAP-07 | 空值哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_sentinel_real` |
| TRAP-06 | 全角千分位 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_fullwidth_decimal` |
| TRAP-20 | 拆并依赖环 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_cycle_real` |
| TRAP-21 | 缺失父级 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_missing_parent_real` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。

## 改单放行层

| 能力 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| 改单提交与裁决 | 提交拆单/并箱/换箱/更正申报变更单 | `POST /api/changes` | `app/changes.py`、`app/changestore.py` | `tests/test_changes.py` |
| 当前指针 | 查看当前批与生效变更 | `GET /api/current` | `app/changestore.py` | `tests/test_changes.py::test_same_change_is_idempotent` |
| 封存件下载 | 下载裁决通过主单的封存件 | `GET /api/changes/{change_id}/seal/{master_id}` | `app/changes.py::seal_zip` | `tests/test_changes.py::test_seal_only_for_passed_master` |

变更单结构错误 422；数值问题随候选批进管线隔离。拆单子票合计不守恒、跨父并箱由净毛重核验阻断；并箱成员单位不一致或危险品类别冲突直接拒绝（BAD_MERGE）。换箱移动整棵子树，危险品同箱按候选批重判。

## 改单陷阱登记

| TRAP | 现象 | 处理 | 位置 | 测试 |
|---|---|---|---|---|
| TRAP-C1 | 改单后旧关系残留 | 候选批整体替换当前指针，页面只展示当前关系，差异单列移除项 | `app/changestore.py::_switch` | `tests/test_changes.py::test_seal_only_for_passed_master` |
| TRAP-C2 | 换箱后危险品说不清 | 子树整箱移动后重判同箱相容；通过/阻断主单同箱牵连整批待行 | `app/changes.py::judge` | `tests/test_changes.py::test_entanglement_forces_batch_hold` |
| TRAP-C3 | 主单申报重量脱节 | 候选批与报关预申报逐项对账，脱节即整批待行 | `app/changes.py::judge` | `tests/test_changes.py::test_declaration_mismatch_holds_batch` |
| TRAP-C4 | 切换半截生效 | 落库与拨指针同事务，失败整体回滚并留修改前证据 | `app/changestore.py::_switch/_record_rollback` | `tests/test_changes.py::test_stale_base_rolls_back_and_keeps_evidence` |
| TRAP-C5 | 重跑变更出现第二套当前结果 | 变更编号由变更单原文确定性生成，重复提交返回既有结果 | `app/changes.py::change_id` | `tests/test_changes.py::test_same_change_is_idempotent` |
| TRAP-C6 | 隔离行随改单丢失 | 隔离行原值随候选批重新隔离，证据不静默消失 | `app/changes.py::apply_changes` | `tests/test_changes.py::test_quarantine_rows_follow_candidate` |
