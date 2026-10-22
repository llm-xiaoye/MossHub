"""Serve the built UI with production handlers and an isolated functional sample vault."""
from __future__ import annotations
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_obsidian_import import ObsidianFunctionalTest, PROJECT
from raghub import ui
from raghub.indexing import router as indexing

fixture = ObsidianFunctionalTest()
try:
    fixture.setUp()
    ui.settings = fixture.settings
    indexing.settings = fixture.settings
    app = fixture.client.app
    app.add_api_route("/api/ui/knowledge-bases", ui.knowledge_bases, methods=["GET"])
    app.add_api_route("/api/ui/documents", ui.documents, methods=["GET"])
    app.add_api_route("/api/ui/tasks", ui.tasks, methods=["GET"])
    app.add_api_route("/api/knowledge-bases/{kb_id}/generations", indexing.generations, methods=["GET"])
    app.mount("/", StaticFiles(directory=PROJECT / "frontend/dist", html=True))
    print("ISOLATED_FUNCTIONAL_UI_ONLY root=" + str(fixture.root) + " instance=" + fixture.instance + " kb=" + fixture.kb + " other_kb=" + fixture.other_kb, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18081, access_log=False)
finally:
    fixture.doCleanups()
