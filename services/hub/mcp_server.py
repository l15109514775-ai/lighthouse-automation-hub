import os
from typing import Literal
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .config import Settings
from .control import Control
from .db import Store


def create_server(settings=None):
    settings = settings or Settings.load()
    settings.prepare()
    store = Store(settings.db_path)
    store.init()
    control = Control(settings, store)
    server = FastMCP('Lighthouse Automation Hub', log_level='WARNING',
                     instructions='Read system state or request scoped maintenance. No publishing or shell tools exist.')
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def principal(write=False):
        # The local launcher binds a token to this stdio session; it is never a tool argument.
        return control.authorize(os.getenv('HUB_MCP_TOKEN', os.getenv('HUB_READ_TOKEN','')), write=write)

    @server.tool(annotations=read)
    def get_system_status() -> dict:
        """Read queues, API budgets, service heartbeats and CPU/memory/disk status; no credentials."""
        principal()
        return control.status()

    @server.tool(annotations=read)
    def list_jobs(state: str | None = None, limit: int = 50) -> list[dict]:
        """List at most 200 persistent jobs, optionally filtered by state."""
        principal()
        return control.list_jobs(state, limit)

    @server.tool(annotations=read)
    def get_job_details(job_id: str) -> dict:
        """Read one UUID job including its stored evidence and missing review requirements."""
        principal()
        return control.get_job(job_id)

    @server.tool(annotations=read)
    def list_failures(limit: int = 50) -> list[dict]:
        """Read failed jobs and categorized error codes; does not retry or publish."""
        principal()
        return control.list_jobs('failed', limit)

    @server.tool(annotations=write)
    def retry_job(job_id: str) -> dict:
        """With a separately bound maintenance credential, retry an eligible failure; unknown publications remain blocked."""
        return control.retry(principal(write=True), job_id)

    @server.tool(annotations=write)
    def pause_pipeline(pipeline: Literal['tweet','video','system']) -> dict:
        """With maintenance permission, stop new claims in a queue and audit the request."""
        return control.pause(principal(write=True), pipeline, True)

    @server.tool(annotations=write)
    def resume_pipeline(pipeline: Literal['tweet','video','system']) -> dict:
        """With maintenance permission, resume a queue; does not grant review or publication approval."""
        return control.pause(principal(write=True), pipeline, False)

    @server.tool(annotations=read)
    def get_daily_report() -> dict:
        """Read today's Beijing-time job, error/event and conservative API usage report."""
        principal()
        return control.daily_report()

    return server


def main():
    create_server().run(transport='stdio')


if __name__ == '__main__':
    main()
