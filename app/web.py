"""Tiny HTTP server behind the direct download links (supports resume via Range)."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .db import Database, now
from .files import content_disposition

GONE = "This link has expired or is invalid.\nاین لینک منقضی شده یا نامعتبر است.\n"


def make_app(db: Database) -> web.Application:
    async def download(request: web.Request) -> web.StreamResponse:
        batch = await db.get_batch_by_token(request.match_info["token"])
        if (
            not batch
            or batch["deleted"]
            or (batch["expires_at"] and batch["expires_at"] < now())
            or not Path(batch["path"]).is_file()
        ):
            return web.Response(status=404, text=GONE, content_type="text/plain", charset="utf-8")
        return web.FileResponse(
            batch["path"],
            headers={
                "Content-Disposition": content_disposition(batch["file_name"]),
                "Content-Type": "application/zip",
                "Cache-Control": "private, no-store",
            },
        )

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/dl/{token}/{name}", download)
    app.router.add_get("/dl/{token}", download)
    app.router.add_get("/health", health)
    return app


async def start_web(db: Database, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(db), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
