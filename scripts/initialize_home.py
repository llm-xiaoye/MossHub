"""Create the documented local layout; never start services or overwrite data."""
from pathlib import Path
import json
from raghub.config import Settings

def main():
    settings = Settings.load()
    if settings.project.resolve() != Path(__file__).resolve().parents[1]:
        raise SystemExit("Clone this repository into RAG_HOME/project first")
    directories = (*settings.required_dirs(), settings.home / "private",
                   settings.home / "obsidian", settings.home / "cache",
                   settings.home / "runtime/browsers")
    for path in directories:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise SystemExit("Directory is aliased or not a directory: " + str(path))
    print(json.dumps({"home": str(settings.home), "directories": len(directories),
                      "services_started": False}))

if __name__ == "__main__":
    main()
