"""改单批次存取：整批结果统一生效、失败完整回滚、同一变更幂等。

- change_orders 记录每张变更单：基准批、候选批、变更内容、裁决结果、状态（生效/回滚）。
- current_manifest 单行表保存当前指针；切换与变更单落库在同一事务内，
  任一步失败整体回滚，旧版当前批不受影响，修改前证据（基准批快照与变更内容）保留。
- 变更编号由基准批与变更内容确定性生成，重复提交同一变更只返回既有结果，
  不会新增第二套当前结果。
"""
import json, sqlite3
from datetime import datetime, timezone
from app import store
from app.changes import normalize, change_id, apply_changes, to_csv, diff_records, judge
from app.engine import evaluate
from app.errors import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS change_orders (
  change_id TEXT PRIMARY KEY,
  base_run_id TEXT NOT NULL,
  candidate_run_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  decision TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_manifest (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  run_id TEXT NOT NULL,
  change_id TEXT
);
"""


def connect(path):
    con = store.connect(path)
    con.executescript(SCHEMA)
    return con


def ensure_current(path):
    with connect(path) as con:
        row = con.execute('SELECT run_id FROM current_manifest WHERE singleton=1').fetchone()
    if row:
        return row['run_id']
    result = store.save(path, evaluate())
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO current_manifest(singleton, run_id, change_id) VALUES(1, ?, NULL)',
                    (result['manifest']['run_id'],))
    return result['manifest']['run_id']


def current(path):
    run_id = ensure_current(path)
    with connect(path) as con:
        row = con.execute('SELECT run_id, change_id FROM current_manifest WHERE singleton=1').fetchone()
    return {'run_id': row['run_id'], 'change_id': row['change_id']}


def get_change(path, cid):
    with connect(path) as con:
        row = con.execute('SELECT * FROM change_orders WHERE change_id=?', (cid,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '变更单不存在', 404)
    return _unpack(row)


def find_change(path, cid):
    with connect(path) as con:
        row = con.execute('SELECT * FROM change_orders WHERE change_id=?', (cid,)).fetchone()
    return _unpack(row) if row else None


def _unpack(row):
    return {'change_id': row['change_id'], 'base_run_id': row['base_run_id'],
            'candidate_run_id': row['candidate_run_id'], 'payload': json.loads(row['payload']),
            'decision': json.loads(row['decision']), 'status': row['status'], 'created_at': row['created_at']}


def list_changes(path):
    with connect(path) as con:
        rows = con.execute('SELECT * FROM change_orders ORDER BY rowid DESC LIMIT 100').fetchall()
    return [_unpack(r) for r in rows]


def submit(path, payload):
    """提交变更单：生成候选批并原子切换当前指针。失败完整回滚并保留修改前证据。

    变更编号由变更单原文确定性生成：同一变更单重复提交只返回既有结果，
    不新增第二套当前结果；显式指定基准批可构成另一张变更单。
    """
    canonical = normalize(payload)
    cid = change_id(canonical)
    existing = find_change(path, cid)
    if existing:
        existing['reused'] = True
        return existing
    base_run_id = canonical.get('base_run_id') or ensure_current(path)
    base = store.get(path, base_run_id)
    if 'input_snapshot' not in base:
        raise BusinessError('INPUT_SNAPSHOT_MISSING', '基准批缺少源快照，不能作为改单基准')
    rows, quarantined = apply_changes(base, canonical)
    csv_text = to_csv(rows + quarantined)
    candidate = evaluate({'dataset': base['source'], 'csv_text': csv_text,
                          'overrides': base['input_snapshot']['context']})
    store.save(path, candidate)
    candidate_run_id = candidate['manifest']['run_id']
    candidate = store.get(path, candidate_run_id)
    decision = judge(base, candidate, diff_records(base, candidate))
    record = {'change_id': cid, 'base_run_id': base_run_id, 'candidate_run_id': candidate_run_id,
              'payload': canonical, 'decision': decision, 'created_at': datetime.now(timezone.utc).isoformat()}
    try:
        _switch(path, record)
    except BusinessError:
        _record_rollback(path, record)
        raise
    except sqlite3.Error as error:
        _record_rollback(path, record, str(error))
        raise BusinessError('SWITCH_FAILED', '切换失败，已完整恢复旧版并保留修改前证据', 500)
    return {**record, 'status': '生效', 'reused': False}


def _switch(path, record):
    """单事务：校验基准仍是当前 → 落变更单 → 拨当前指针。任一步失败整体回滚。"""
    with connect(path) as con:
        row = con.execute('SELECT run_id FROM current_manifest WHERE singleton=1').fetchone()
        if not row or row['run_id'] != record['base_run_id']:
            raise BusinessError('BASE_STALE', '基准版本已被其他变更替换，切换已回滚，旧版保持当前', 409)
        con.execute('INSERT INTO change_orders(change_id, base_run_id, candidate_run_id, payload, decision, status, created_at)'
                    ' VALUES(?,?,?,?,?,?,?)',
                    (record['change_id'], record['base_run_id'], record['candidate_run_id'],
                     json.dumps(record['payload'], ensure_ascii=False),
                     json.dumps(record['decision'], ensure_ascii=False), '生效', record['created_at']))
        con.execute('UPDATE current_manifest SET run_id=?, change_id=? WHERE singleton=1',
                    (record['candidate_run_id'], record['change_id']))


def _record_rollback(path, record, error='BASE_STALE'):
    """切换失败：当前指针不动（旧版完整保留），变更单以回滚状态留证。"""
    decision = {**record['decision'], 'rolled_back': True, 'error': error,
                'preserved': {'base_run_id': record['base_run_id'], 'candidate_run_id': record['candidate_run_id']}}
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO change_orders(change_id, base_run_id, candidate_run_id, payload, decision, status, created_at)'
                    ' VALUES(?,?,?,?,?,?,?)',
                    (record['change_id'], record['base_run_id'], record['candidate_run_id'],
                     json.dumps(record['payload'], ensure_ascii=False),
                     json.dumps(decision, ensure_ascii=False), '回滚', record['created_at']))
