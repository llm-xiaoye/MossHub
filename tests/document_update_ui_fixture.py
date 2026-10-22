"""Isolated update UI: sample files and local SQLite, no providers or broker."""
from __future__ import annotations
import json
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_obsidian_import import ObsidianFunctionalTest, PROJECT
from raghub import ui
from raghub.indexing import router as indexing

fixture = ObsidianFunctionalTest()
try:
    fixture.setUp()
    original = fixture.initial_document()
    other = fixture.initial_document("notes/002.md")
    local = fixture.root / "local-update"
    (local / "assets").mkdir(parents=True)
    (local / "replacement.md").write_text("# Local update sample\n![image](assets/picture.png)\n")
    (local / "unselected.md").write_text("Unselected sample")
    (local / "assets/picture.png").write_bytes((fixture.vault / "assets/picture.png").read_bytes())
    ui.settings = fixture.settings
    indexing.settings = fixture.settings
    app = fixture.client.app
    app.add_api_route("/api/ui/knowledge-bases", ui.knowledge_bases, methods=["GET"])
    app.add_api_route("/api/ui/documents", ui.documents, methods=["GET"])
    app.add_api_route("/api/ui/tasks", ui.tasks, methods=["GET"])
    app.add_api_route("/api/knowledge-bases/{kb_id}/generations", indexing.generations, methods=["GET"])
    app.mount("/", StaticFiles(directory=PROJECT / "frontend/dist", html=True))
    print(json.dumps({"scope":"ISOLATED_UPDATE_UI","root":str(fixture.root),"local_directory":str(local),
                      "instance":fixture.instance,"kb":fixture.kb,"original":original["document_id"],
                      "other":other["document_id"]}), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18082, access_log=False)
finally:
    fixture.doCleanups()

