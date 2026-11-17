"""改单放行页与接口。

GET  /changes                              页面：当前关系、阻断原因、修改前后差异、整批裁决
POST /api/changes                          提交变更单（幂等；整批统一生效，失败回滚留证）
GET  /api/changes                          变更单历史
GET  /api/changes/{change_id}              变更单详情
GET  /api/changes/{change_id}/seal/{master_id}  封存件下载（仅裁决通过的主单）
GET  /api/current                          当前指针
"""
import json
from fastapi import APIRouter, Request, Body
from fastapi.responses import Response
from app.config import ROOT, META
from app.web import templates
from app import changestore
from app.changes import seal_zip

router = APIRouter()


def _db(request):
    from app.main import DB_PATH
    return DB_PATH


@router.get('/changes')
def page(request: Request):
    from app import store
    path = _db(request)
    pointer = changestore.current(path)
    state = store.get(path, pointer['run_id'])
    history = changestore.list_changes(path)
    selected = request.query_params.get('change_id')
    change = None
    if selected:
        change = changestore.get_change(path, selected)
    elif history:
        change = history[0]
    samples = {name: (ROOT / 'sample_data' / f'change_{name}.json').read_text()
               for name in ('fix', 'break')}
    return templates.TemplateResponse(request=request, name='changes.html', context={
        'meta': META, 'key': 'changes', 'state': state, 'view': {'rows': []},
        'pointer': pointer, 'change': change, 'history': history, 'samples': samples})


@router.post('/api/changes')
def create(request: Request, payload: dict = Body(...)):
    return changestore.submit(_db(request), payload)


@router.get('/api/changes')
def history(request: Request):
    return changestore.list_changes(_db(request))


@router.get('/api/changes/{change_id}')
def detail(request: Request, change_id: str):
    return changestore.get_change(_db(request), change_id)


@router.get('/api/changes/{change_id}/seal/{master_id}')
def seal(request: Request, change_id: str, master_id: str):
    change = changestore.get_change(_db(request), change_id)
    content = seal_zip(change, master_id)
    return Response(content, media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="seal-{change_id}-{master_id}.zip"'})


@router.get('/api/current')
def pointer(request: Request):
    return changestore.current(_db(request))
