"""Serve the real UI/routes against an isolated 51-note functional sample vault."""
from __future__ import annotations
import sys
from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_obsidian_import import ObsidianFunctionalTest, PROJECT
from raghub import ui
from raghub.indexing import router as indexing

fixture = ObsidianFunctionalTest()
fixture.setUp()
ui.settings = fixture.settings
indexing.settings = fixture.settings
app = fixture.client.app
ordinary_ui = APIRouter()
for route in ui.router.routes:
    if not route.path.startswith("/api/ui/obsidian/"):
        ordinary_ui.routes.append(route)
app.include_router(ordinary_ui)
app.include_router(indexing.router)
app.mount("/", StaticFiles(directory=PROJECT / "frontend/dist", html=True))
print("ISOLATED_FUNCTIONAL_UI_ONLY root=" + str(fixture.root) + " instance=" + fixture.instance + " kb=" + fixture.kb, flush=True)
try:
    uvicorn.run(app, host="127.0.0.1", port=18081, access_log=False)
finally:
    fixture.doCleanups()
