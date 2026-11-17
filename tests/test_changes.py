"""改单放行：拆并链守恒、危险品同箱、主单申报重量、整批统一生效与幂等。"""
import io, json, zipfile
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import main, changestore
from app.changes import normalize, apply_changes, to_csv, diff_records
from app.engine import evaluate
from app.errors import BusinessError


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


@pytest.fixture
def c(tmp_path, monkeypatch):
    return client(tmp_path, monkeypatch)


def current(c):
    return c.get('/api/current').json()


def test_split_conserves_and_master_passes(c):
    payload = {'changes': [{'op': 'split', 'id': 'M002-P1-2', 'into': [
        {'id': 'M002-P1-2A', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't'},
        {'id': 'M002-P1-2B', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't'}]}]}
    result = c.post('/api/changes', json=payload).json()
    m002 = next(m for m in result['decision']['masters'] if m['id'] == 'M002')
    assert m002['verdict'] == '通过'
    assert any(a['id'] == 'M002-P1-2A' for a in m002['diff']['added'])
    assert any(r['id'] == 'M002-P1-2' for r in m002['diff']['removed'])


def test_same_change_is_idempotent(c):
    payload = {'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]}
    a = c.post('/api/changes', json=payload).json()
    b = c.post('/api/changes', json=payload).json()
    assert a['change_id'] == b['change_id'] and b['reused'] is True
    assert len(c.get('/api/changes').json()) == 1
    assert current(c)['run_id'] == a['candidate_run_id']
    assert current(c)['change_id'] == a['change_id']


def test_split_breaking_conservation_blocks_master(c):
    payload = {'changes': [{'op': 'split', 'id': 'M002-P1', 'into': [
        {'id': 'M002-P1X', 'packages': '12', 'net': '120', 'gross': '132'},
        {'id': 'M002-P1Y', 'packages': '6', 'net': '60', 'gross': '66'}]}]}
    result = c.post('/api/changes', json=payload).json()
    m002 = next(m for m in result['decision']['masters'] if m['id'] == 'M002')
    assert m002['verdict'] == '阻断'
    assert any(r['code'] == 'WEIGHT_MISMATCH' for r in m002['reasons'])
    seal = c.get(f"/api/changes/{result['change_id']}/seal/M002")
    assert seal.status_code == 422 and '不能放行' in seal.json()['detail']


def test_swap_moves_whole_subtree(c):
    payload = {'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]}
    result = c.post('/api/changes', json=payload).json()
    run = c.get('/api/runs/' + result['candidate_run_id']).json()
    moved = {r['id']: r['container'] for r in run['records'] if r['id'].startswith('M001-P2')}
    assert moved and set(moved.values()) == {'C009'}
    m001 = next(m for m in result['decision']['masters'] if m['id'] == 'M001')
    assert 'C009' in m001['containers']


def test_swap_into_incompatible_container_blocks(c):
    payload = {'changes': [
        {'op': 'split', 'id': 'M001-P1-2', 'into': [
            {'id': 'M001-P1-2A', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't', 'hazard': '3', 'un_code': 'UN1993'},
            {'id': 'M001-P1-2B', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't', 'hazard': 'none'}]},
        {'op': 'swap', 'id': 'M001-P2', 'container': 'C009'},
        {'op': 'split', 'id': 'M001-P2-1', 'into': [
            {'id': 'M001-P2-1A', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't', 'hazard': '5.1', 'un_code': 'UN1479', 'container': 'C001'},
            {'id': 'M001-P2-1B', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't'}]}]}
    result = c.post('/api/changes', json=payload).json()
    m001 = next(m for m in result['decision']['masters'] if m['id'] == 'M001')
    assert m001['verdict'] == '阻断'
    assert any(r['code'] == 'INCOMPATIBLE' for r in m001['reasons'])


def test_merge_conserves_and_cross_parent_merge_blocks(c):
    ok = c.post('/api/changes', json={'changes': [{'op': 'merge', 'ids': ['M002-P1-1', 'M002-P1-2'], 'into': {'id': 'M002-P1-1'}}]}).json()
    m002 = next(m for m in ok['decision']['masters'] if m['id'] == 'M002')
    assert m002['verdict'] == '通过'
    merged = next(t for t in m002['tree'] if t['id'] == 'M002-P1-1')
    assert merged['pieces'] == 20 and Decimal(merged['net_kg']) == 200
    bad = c.post('/api/changes', json={'changes': [{'op': 'merge', 'ids': ['M001-P1', 'M002-P1']}]}).json()
    verdicts = {m['id']: m['verdict'] for m in bad['decision']['masters']}
    assert verdicts['M001'] == '阻断' and verdicts['M002'] == '阻断'


def test_merge_conflicting_hazard_rejected(c):
    c.post('/api/changes', json={'changes': [{'op': 'split', 'id': 'M001-P1-2', 'into': [
        {'id': 'M001-P1-2A', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't', 'hazard': '3', 'un_code': 'UN1993'},
        {'id': 'M001-P1-2B', 'packages': '5', 'package_unit': 'carton', 'net': '0.05', 'gross': '0.055', 'weight_unit': 't', 'hazard': '5.1', 'un_code': 'UN1479'}]}]})
    response = c.post('/api/changes', json={'changes': [{'op': 'merge', 'ids': ['M001-P1-2A', 'M001-P1-2B']}]})
    assert response.status_code == 422 and response.json()['code'] == 'BAD_MERGE'


def test_stale_base_rolls_back_and_keeps_evidence(c):
    first = c.post('/api/changes', json={'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]}).json()
    base_before = first['base_run_id']
    assert current(c)['run_id'] == first['candidate_run_id']
    stale = c.post('/api/changes', json={'base_run_id': base_before,
                                         'changes': [{'op': 'swap', 'id': 'M002-P2', 'container': 'C010'}]})
    assert stale.status_code == 409 and stale.json()['code'] == 'BASE_STALE'
    assert current(c)['run_id'] == first['candidate_run_id']
    record = c.get('/api/changes').json()
    rolled = next(h for h in record if h['status'] == '回滚')
    assert rolled['base_run_id'] == base_before
    assert rolled['decision']['rolled_back'] is True
    assert rolled['decision']['preserved']['base_run_id'] == base_before
    assert c.get(f"/api/changes/{rolled['change_id']}/seal/M002").status_code == 409


def test_seal_only_for_passed_master(c):
    fix = json.loads(open('sample_data/change_fix.json').read())
    result = c.post('/api/changes', json=fix).json()
    assert result['decision']['batch'] == '部分放行'
    seal = c.get(f"/api/changes/{result['change_id']}/seal/M001")
    assert seal.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(seal.content))
    assert {'manifest.json', 'current.json', 'diff.json', 'verdict.json', 'evidence.json'} <= set(bundle.namelist())
    tree = json.loads(bundle.read('current.json'))
    assert any(row['id'] == 'M001-P1-2A' for row in tree)
    assert not any(row['id'] == 'M001-P1-2' for row in tree)


def test_entanglement_forces_batch_hold(c):
    payload = json.loads(open('sample_data/change_break.json').read())
    result = c.post('/api/changes', json=payload).json()
    assert result['decision']['batch'] == '整批待行'
    assert 'C002' in result['decision']['entangled']
    verdicts = {m['id']: m['verdict'] for m in result['decision']['masters']}
    assert verdicts['M002'] == '阻断' and verdicts['M003'] == '通过'


def test_declaration_mismatch_holds_batch(c):
    payload = {'changes': [{'op': 'redeclare', 'id': 'M001', 'gross': '999'}]}
    result = c.post('/api/changes', json=payload).json()
    assert result['decision']['batch'] == '整批待行'
    assert any(b['code'] == 'DECLARATION_MISMATCH' for b in result['decision']['blockers'])


def test_unknown_reference_rejected(c):
    response = c.post('/api/changes', json={'changes': [{'op': 'swap', 'id': 'NOPE', 'container': 'C009'}]})
    assert response.status_code == 422 and response.json()['code'] == 'UNKNOWN_REFERENCE'


def test_change_page_lists_verdicts(c):
    c.post('/api/changes', json={'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]})
    html = c.get('/changes').text
    assert '改单放行' in html and '当前指针' in html
    change = c.get('/api/changes').json()[0]
    detail = c.get('/changes?change_id=' + change['change_id']).text
    assert change['change_id'] in detail and '生效' in detail


def test_quarantine_rows_follow_candidate(c):
    result = c.post('/api/changes', json={'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]}).json()
    run = c.get('/api/runs/' + result['candidate_run_id']).json()
    assert len(run['quarantine']) == 4
    codes = {q['code'] for q in run['quarantine']}
    assert 'DUPLICATE_CONFLICT' in codes


def test_normalize_rejects_bad_payload():
    with pytest.raises(BusinessError):
        normalize({'changes': []})
    with pytest.raises(BusinessError):
        normalize({'changes': [{'op': 'teleport', 'id': 'M001'}]})
    with pytest.raises(BusinessError):
        normalize({'changes': [{'op': 'split', 'id': 'M001', 'into': [{'id': 'A'}]}]})


def test_diff_tracks_fields():
    base = evaluate({'dataset': 'clean'})
    canonical = normalize({'changes': [{'op': 'swap', 'id': 'M001-P2', 'container': 'C009'}]})
    rows, quarantined = apply_changes(base, canonical)
    candidate = evaluate({'dataset': 'clean', 'csv_text': to_csv(rows + quarantined)})
    diff = diff_records(base, candidate)
    changed = {(d['id'], d['field']) for d in diff['changed']}
    assert ('M001-P2', 'container') in changed
    entry = next(d for d in diff['changed'] if d['id'] == 'M001-P2' and d['field'] == 'container')
    assert entry['before'] == 'C001' and entry['after'] == 'C009'
