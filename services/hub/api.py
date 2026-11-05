from contextlib import asynccontextmanager
import time
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from .config import Settings
from .control import Control
from .db import Store
from .errors import HubError


def create_app(settings=None):
    settings = settings or Settings.load()
    settings.prepare()
    store = Store(settings.db_path)
    store.init()
    control = Control(settings, store)
    app = FastAPI(title='Lighthouse Automation Hub', docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(HubError)
    async def hub_error(request, exc):
        status = 403 if exc.code in ('ACCESS_DENIED','TOKENS_MUST_BE_DISTINCT') else 400
        return JSONResponse(status_code=status, content={'error_code': exc.code})

    def token(authorization: str = Header(default='')):
        return authorization[7:] if authorization.startswith('Bearer ') else ''

    def reader(value=Depends(token)):
        return control.authorize(value)

    def maintainer(value=Depends(token)):
        return control.authorize(value, write=True)

    @app.get('/health')
    def health():
        return {'status': 'alive'}

    @app.get('/ready')
    def ready():
        try:
            store.query('SELECT version FROM schema_migrations')
            beats = {row['name']: row['at'] for row in store.query('SELECT * FROM heartbeats')}
            stale = [name for name in ('scheduler','worker:tweet','worker:video','worker:system')
                     if time.time() - beats.get(name, 0) > 120]
            return JSONResponse(status_code=503 if stale else 200,
                                content={'status': 'degraded' if stale else 'ready', 'stale_services': stale})
        except Exception:
            return JSONResponse(status_code=503, content={'status': 'unready', 'error_code': 'DATABASE_UNAVAILABLE'})

    @app.get('/status', dependencies=[Depends(reader)])
    def status():
        return control.status()

    @app.get('/automation-readiness', dependencies=[Depends(reader)])
    def automation_readiness():
        from .readiness import automation_readiness as inspect
        return inspect(settings, store)

    @app.get('/jobs', dependencies=[Depends(reader)])
    def jobs(state: str | None = None, limit: int = Query(default=50, ge=1, le=200)):
        return control.list_jobs(state, limit)

    @app.get('/jobs/{job_id}', dependencies=[Depends(reader)])
    def job_details(job_id: str):
        return control.get_job(job_id)

    @app.get('/failures', dependencies=[Depends(reader)])
    def failures():
        return control.list_jobs('failed')

    @app.get('/daily-report', dependencies=[Depends(reader)])
    def report():
        return control.daily_report()

    @app.post('/events/rpc')
    def events_rpc(value: dict, access_token=Depends(token)):
        # Local protocol prototype. Remote MCP registration/OAuth is a separate deployment step.
        from .events import Events
        method = value.get('method')
        owner = control.authorize(access_token, write=method != 'events/list')
        service = Events(settings, store)
        params = value.get('params', {})
        if method == 'events/list':
            result = service.catalog()
        elif method == 'events/subscribe':
            result = service.subscribe(owner, params.get('name'), params.get('arguments', {}),
                params.get('delivery', {}), ttl_ms=params.get('ttlMs', 86400000), cursor=params.get('cursor'))
        elif method == 'events/unsubscribe':
            result = service.unsubscribe(owner, params.get('id', ''))
        else:
            raise HubError('EVENT_METHOD_UNSUPPORTED')
        store.audit(owner, method, 'event-subscriptions', 'ok')
        return {'jsonrpc':'2.0','id':value.get('id'),'result':result}

    @app.post('/jobs/{job_id}/retry')
    def retry_job(job_id: str, principal=Depends(maintainer)):
        return control.retry(principal, job_id)

    class PipelineRequest(BaseModel):
        model_config = ConfigDict(extra='forbid')
        paused: bool

    @app.post('/pipelines/{queue}')
    def pipeline(queue: Literal['tweet','video','system'], value: PipelineRequest, principal=Depends(maintainer)):
        return control.pause(principal, queue, value.paused)

    return app
