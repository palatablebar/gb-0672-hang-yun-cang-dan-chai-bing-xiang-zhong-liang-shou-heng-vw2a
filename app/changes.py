"""改单领域逻辑：拆单、并箱、换箱、更正申报。

变更作用于当前批次的采信记录，生成候选舱单后重新走全流程核验。
拆并链守恒、危险品同箱、主单申报重量由管线重新判定，本模块负责：
- 变更单结构校验与确定性编号（同一变更重复提交得到同一编号）
- 候选行集生成（旧关系被新关系整体替换，隔离行随批保留证据）
- 修改前后差异、每张主提单的当前关系与阻断原因、整批放行裁决
"""
import csv, io, json, hashlib
from copy import deepcopy
from decimal import Decimal
from app.errors import BusinessError

FIELDS = ['id', 'parent', 'packages', 'package_unit', 'net', 'gross', 'weight_unit', 'hazard', 'un_code', 'container', 'revision']
OPS = {'split', 'merge', 'swap', 'redeclare'}
TRACKED = ['parent', 'container', 'pieces', 'net_kg', 'gross_kg', 'hazard', 'un_code', 'revision']


def change_id(canonical):
    """变更编号 = 规范化变更单（含显式基准批）的摘要。

    同一变更单重复提交得到同一编号，只返回既有结果，
    不会新增第二套当前结果；显式指定不同基准批则构成另一张变更单。
    """
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def normalize(payload):
    """校验并规范化变更单。结构错误直接 422，数值问题留给管线隔离。"""
    if not isinstance(payload, dict):
        raise BusinessError('BAD_CHANGE', '变更单须为对象')
    changes = payload.get('changes')
    if not isinstance(changes, list) or not changes:
        raise BusinessError('EMPTY_CHANGES', '变更单至少包含一条变更')
    base_run_id = payload.get('base_run_id')
    if base_run_id is not None and not isinstance(base_run_id, str):
        raise BusinessError('BAD_CHANGE', '基准批号须为字符串')
    for item in changes:
        if not isinstance(item, dict) or item.get('op') not in OPS:
            raise BusinessError('BAD_CHANGE', f'变更类型须为 {sorted(OPS)}')
        op = item['op']
        if op == 'split':
            parts = item.get('into')
            if not isinstance(item.get('id'), str) or not item['id'].strip():
                raise BusinessError('BAD_CHANGE', '拆单缺少目标编号')
            if not isinstance(parts, list) or len(parts) < 2:
                raise BusinessError('BAD_CHANGE', '拆单至少拆成两票')
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get('id'), str) or not part['id'].strip():
                    raise BusinessError('BAD_CHANGE', '拆分票缺少新编号')
        elif op == 'merge':
            ids = item.get('ids')
            if not isinstance(ids, list) or len(ids) < 2 or len({str(i) for i in ids}) != len(ids):
                raise BusinessError('BAD_CHANGE', '并箱至少选择两个不同编号')
            if item.get('into') is not None and not isinstance(item['into'], dict):
                raise BusinessError('BAD_CHANGE', '并箱目标须为对象')
        elif op == 'swap':
            if not isinstance(item.get('id'), str) or not item['id'].strip():
                raise BusinessError('BAD_CHANGE', '换箱缺少目标编号')
            if not isinstance(item.get('container'), str) or not item['container'].strip():
                raise BusinessError('BAD_CHANGE', '换箱缺少目标箱号')
        elif op == 'redeclare':
            if not isinstance(item.get('id'), str) or not item['id'].strip():
                raise BusinessError('BAD_CHANGE', '更正申报缺少目标编号')
            if not any(k in item for k in ('packages', 'net', 'gross')):
                raise BusinessError('BAD_CHANGE', '更正申报至少给出件数、净重或毛重')
    canonical = {'changes': deepcopy(changes)}
    if base_run_id:
        canonical['base_run_id'] = base_run_id
    return canonical


def _rows_by_id(rows):
    return {r['id']: r for r in rows}


def _require(rows, bill_id):
    row = rows.get(bill_id)
    if row is None:
        raise BusinessError('UNKNOWN_REFERENCE', f'编号 {bill_id} 不在当前批次采信记录中')
    return row


def _bump(row):
    row['revision'] = int(row['revision']) + 1


def _fresh(base_row, new_id):
    row = {k: base_row.get(k, '') for k in FIELDS}
    row['id'] = new_id
    row['pieces'] = int(base_row['pieces'])
    row['net_kg'] = str(base_row['net_kg'])
    row['gross_kg'] = str(base_row['gross_kg'])
    return row


def _split(rows, order, item):
    target = _require(rows, item['id'])
    part_ids = [p['id'].strip() for p in item['into']]
    if len(set(part_ids)) != len(part_ids) or any(p in rows for p in part_ids):
        raise BusinessError('DUPLICATE_CONFLICT', '拆分新编号与现有编号冲突')
    keeper = next((p['id'].strip() for p in item['into'] if p.get('keep_children')), part_ids[0])
    parts = []
    for spec in item['into']:
        row = _fresh(target, spec['id'].strip())
        for key in ('packages', 'package_unit', 'net', 'gross', 'weight_unit', 'hazard', 'un_code', 'container'):
            if key in spec:
                row[key] = str(spec[key])
        _bump(row)
        parts.append(row)
    for child in [r for r in order if r['parent'] == target['id']]:
        child['parent'] = keeper
        _bump(child)
    index = order.index(target)
    order[index:index + 1] = parts
    for row in parts:
        rows[row['id']] = row
    del rows[target['id']]


def _merge(rows, order, item):
    ids = [str(i) for i in item['ids']]
    members = [_require(rows, i) for i in ids]
    into = item.get('into') or {}
    result_id = str(into.get('id') or ids[0])
    if result_id not in ids and result_id in rows:
        raise BusinessError('DUPLICATE_CONFLICT', f'并箱目标编号 {result_id} 已被占用')
    if len({m['package_unit'] for m in members}) > 1 or len({m['weight_unit'] for m in members}) > 1:
        raise BusinessError('BAD_MERGE', '并箱成员包装或重量单位不一致，无法直接合计')
    classes = {m['hazard'] for m in members if m['hazard'] != 'none'}
    if len(classes) > 1:
        raise BusinessError('BAD_MERGE', '并箱成员危险品类别冲突，合票后危险品说不清')
    first = members[0]
    result = _fresh(first, result_id)
    result['packages'] = str(sum(Decimal(str(m['packages'])) for m in members))
    result['net'] = str(sum(Decimal(str(m['net'])) for m in members))
    result['gross'] = str(sum(Decimal(str(m['gross'])) for m in members))
    result['hazard'] = classes.pop() if classes else 'none'
    result['un_code'] = next((m['un_code'] for m in members if m['un_code']), '')
    result['container'] = str(into.get('container') or first['container'])
    result['revision'] = max(int(m['revision']) for m in members)
    _bump(result)
    member_ids = set(ids) - {result_id}
    for child in order:
        if child['parent'] in member_ids:
            child['parent'] = result_id
            _bump(child)
    index = min(order.index(m) for m in members)
    kept = [r for r in order if r['id'] not in member_ids and r['id'] != result_id]
    kept.insert(index, result)
    order[:] = kept
    for mid in member_ids:
        rows.pop(mid, None)
    rows[result_id] = result


def _swap(rows, order, item):
    target = _require(rows, item['id'])
    container = item['container'].strip()
    children = {}
    for row in order:
        children.setdefault(row['parent'], []).append(row['id'])
    stack, subtree = [target['id']], set()
    while stack:
        node = stack.pop()
        if node in subtree:
            continue
        subtree.add(node)
        stack.extend(children.get(node, []))
    for row in order:
        if row['id'] in subtree and row['container'] != container:
            row['container'] = container
            _bump(row)


def _redeclare(rows, order, item):
    target = _require(rows, item['id'])
    for key in ('packages', 'net', 'gross'):
        if key in item:
            target[key] = str(item[key])
    _bump(target)


def apply_changes(state, canonical):
    """在当前批采信记录上顺序应用变更，返回候选行与随批保留的隔离行原值。"""
    order = [dict(r) for r in state['records']]
    rows = _rows_by_id(order)
    for item in canonical['changes']:
        {'split': _split, 'merge': _merge, 'swap': _swap, 'redeclare': _redeclare}[item['op']](rows, order, item)
    quarantined = [dict(q['raw']) for q in state['quarantine']]
    return order, quarantined


def to_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(FIELDS)
    for row in rows:
        writer.writerow([str(row.get(f, '')) for f in FIELDS])
    return buffer.getvalue()


def _brief(row):
    return {k: row.get(k, '') for k in ('id', 'parent', 'container', 'pieces', 'net_kg', 'gross_kg', 'hazard', 'un_code', 'revision')}


def diff_records(base, candidate):
    """修改前后差异：新增、移除、逐字段变化。"""
    before, after = _rows_by_id(base['records']), _rows_by_id(candidate['records'])
    changed = []
    for bill_id in sorted(before.keys() & after.keys()):
        for field in TRACKED:
            old, new = str(before[bill_id].get(field, '')), str(after[bill_id].get(field, ''))
            if old != new:
                changed.append({'id': bill_id, 'field': field, 'before': old, 'after': new})
    return {'added': [_brief(after[i]) for i in sorted(after.keys() - before.keys())],
            'removed': [_brief(before[i]) for i in sorted(before.keys() - after.keys())],
            'changed': changed}


def subtree_ids(graph, root):
    stack, seen = [root], set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph['children'].get(node, []))
    return seen


def _tree(graph, records, root):
    rows = _rows_by_id(records)
    flat, stack = [], [(root, 0)]
    while stack:
        node, depth = stack.pop()
        row = rows.get(node)
        if row:
            flat.append({'depth': depth, **_brief(row)})
        for child in sorted(graph['children'].get(node, []), reverse=True):
            stack.append((child, depth + 1))
    return flat


def _root_of(node, root_of, rows):
    guard = 0
    while node and guard < 1000:
        if node in root_of:
            return root_of[node]
        row = rows.get(node)
        if row is None:
            return None
        node, guard = row['parent'], guard + 1
    return None


def judge(base, candidate, diff):
    """逐主单放行裁决 + 整批裁决（能否继续报关）。"""
    graph, records = candidate['graph'], candidate['records']
    rows = _rows_by_id(records)
    root_of = {}
    for root in graph['roots']:
        for nid in subtree_ids(graph, root):
            root_of[nid] = root
    quarantine_by_root, floating_quarantine = {}, []
    for q in candidate['quarantine']:
        root = _root_of(str(q.get('raw', {}).get('parent', '')), root_of, rows)
        (quarantine_by_root.setdefault(root, []).append(q) if root else floating_quarantine.append(q))
    masters, covered = [], set()
    for root in graph['roots']:
        ids = subtree_ids(graph, root)
        covered |= ids
        containers = sorted({rows[i]['container'] for i in ids if i in rows})
        base_ids = subtree_ids(base['graph'], root) if root in _rows_by_id(base['records']) else set()
        relevant = ids | base_ids
        reasons = []
        for error in graph['errors']:
            if error['id'] in relevant:
                reasons.append({'code': error['code'], 'id': error['id'],
                                'detail': f"拆并链关系错误：{'/'.join(error.get('path', []))}"})
        for check in candidate['weight_checks']:
            if check['id'] in ids and check['code'] != 'BALANCED':
                reasons.append({'code': 'WEIGHT_MISMATCH', 'id': check['id'],
                                'detail': f"{check['field']} 申报 {check['declared']} ≠ 子单合计 {check['children_sum']}（差 {check['difference']}）"})
        for issue in candidate['hazard_issues']:
            if issue['code'] == 'INCOMPATIBLE':
                if issue['id'] in containers:
                    reasons.append({'code': 'INCOMPATIBLE', 'id': issue['id'],
                                    'detail': f"箱 {issue['id']} 危险品同箱不相容 {issue['classes']}"})
            elif issue['id'] in relevant:
                reasons.append({'code': issue['code'], 'id': issue['id'], 'detail': '危险品类别或UN编号说不清'})
        for q in quarantine_by_root.get(root, []):
            reasons.append({'code': q['code'], 'id': q['id'], 'detail': f"链上原始行被隔离：{q['detail']}"})
        masters.append({'id': root, 'verdict': '阻断' if reasons else '通过', 'reasons': reasons,
                        'containers': containers, 'tree': _tree(graph, records, root),
                        'diff': _diff_for(diff, relevant)})
    floating = [e for e in graph['errors'] if e['id'] not in covered]
    lost = [r for r in base['graph']['roots'] if r not in set(graph['roots'])]
    blockers = ([{'code': 'MASTER_LOST', 'id': r, 'detail': '改单后主单灭失'} for r in lost]
                + [{'code': e['code'], 'id': e['id'], 'detail': f"游离关系错误：{'/'.join(e.get('path', []))}"} for e in floating]
                + [{'code': q['code'], 'id': q['id'], 'detail': f"隔离行未清：{q['detail']}"} for q in floating_quarantine])
    declaration = candidate['balance']['declaration_differences']
    if declaration:
        blockers.append({'code': 'DECLARATION_MISMATCH', 'id': '报关预申报',
                         'detail': f"主单申报总量脱节：{','.join(declaration)}"})
    verdict_of = {m['id']: m['verdict'] for m in masters}
    entangled = []
    by_container = {}
    for row in records:
        by_container.setdefault(row['container'], set()).add(root_of.get(row['id']))
    for container, roots in sorted(by_container.items()):
        verdicts = {verdict_of.get(r) for r in roots if r}
        if '通过' in verdicts and '阻断' in verdicts:
            entangled.append(container)
    passed = [m for m in masters if m['verdict'] == '通过']
    if declaration:
        batch, criterion = '整批待行', '主单申报总量与报关预申报脱节，报关无法继续，等整批处理完再走'
    elif not passed:
        batch, criterion = '整批待行', '没有主单核对通过，报关行拿不到可申报舱单'
    elif entangled:
        batch, criterion = '整批待行', f"箱 {','.join(entangled)} 内通过与阻断主单混装，危险品同箱说不清，不能单独放行"
    elif len(passed) == len(masters) and not blockers:
        batch, criterion = '整批放行', '全部主单链守恒、危险品清、申报平，整批结果统一生效'
    else:
        batch, criterion = '部分放行', '通过的主单各自可独立申报，只放行核对通过的主单，其余等处理完再走'
    return {'batch': batch, 'criterion': criterion, 'entangled': entangled,
            'blockers': blockers, 'masters': masters,
            'totals': {'passed': len(passed), 'blocked': len(masters) - len(passed)}}


def _diff_for(diff, relevant):
    return {'added': [a for a in diff['added'] if a['id'] in relevant],
            'removed': [r for r in diff['removed'] if r['id'] in relevant],
            'changed': [c for c in diff['changed'] if c['id'] in relevant]}


def seal_zip(change, master_id):
    """封存件：仅裁决通过的主单可下载，含当前关系、修改前后差异与判定证据。"""
    import zipfile
    from datetime import datetime, timezone
    if change['status'] != '生效':
        raise BusinessError('CHANGE_NOT_EFFECTIVE', '变更未生效，不能下载封存件', 409)
    master = next((m for m in change['decision']['masters'] if m['id'] == master_id), None)
    if master is None:
        raise BusinessError('NOT_FOUND', '主单不在该变更结果中', 404)
    if master['verdict'] != '通过':
        raise BusinessError('MASTER_BLOCKED', '主单核对未通过，不能放行：' + '；'.join(r['detail'] for r in master['reasons']), 422)
    evidence = {'master': master_id, 'tree': master['tree'], 'diff': master['diff'],
                'reasons': master['reasons'], 'containers': master['containers']}
    seal = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps({'change_id': change['change_id'], 'base_run_id': change['base_run_id'],
                                                'candidate_run_id': change['candidate_run_id'], 'master': master_id,
                                                'batch': change['decision']['batch'], 'seal': seal,
                                                'generated_at': datetime.now(timezone.utc).isoformat()},
                                               ensure_ascii=False, indent=2))
        z.writestr('current.json', json.dumps(master['tree'], ensure_ascii=False, indent=2))
        z.writestr('diff.json', json.dumps(master['diff'], ensure_ascii=False, indent=2))
        z.writestr('verdict.json', json.dumps({'verdict': master['verdict'], 'criterion': change['decision']['criterion']},
                                              ensure_ascii=False, indent=2))
        z.writestr('evidence.json', json.dumps(evidence, ensure_ascii=False, indent=2))
    return buffer.getvalue()
