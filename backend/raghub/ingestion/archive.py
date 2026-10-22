"""Closed, hashed archive members and application-level immutable adoption."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from html.parser import HTMLParser
from uuid import UUID
from urllib.parse import urljoin

from .contracts import FILE_LIMIT, relative_path
from ..config import Settings


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def json_hash(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def uuid_text(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("a canonical UUIDv4 is required")
    return value


def inside(path: Path, root: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root):
        raise RuntimeError("project path is aliased or outside its declared root")
    return path


def make_directory(path: Path, root: Path, *, exclusive: bool = False) -> None:
    inside(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    inside(path.parent, root)
    path.mkdir(exist_ok=not exclusive)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("directory has an unexpected object type")


def write_closed(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def hash_file(path: Path, limit: int = 200 * 1024 * 1024) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("archive members must be REGULAR")
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(256 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("archive member exceeds its byte limit")
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                 item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or size != after.st_size:
            raise RuntimeError("input changed while being read")
    return digest.hexdigest(), size


def copy_closed(source: Path, destination: Path, expected_sha256: str, limit: int) -> int:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as original, destination.open("xb") as output:
        before = os.fstat(original.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("input is not a regular file")
        while chunk := original.read(256 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("file exceeds its byte limit")
            output.write(chunk)
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
        after = os.fstat(original.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError("source changed during snapshot")
    if digest.hexdigest() != expected_sha256 or size != before.st_size:
        raise ValueError("input bytes differ from the accepted upload")
    return size


def link_blob(settings: Settings, source: Path, destination: Path, digest: str, size: int) -> None:
    """Deduplicate bytes while preserving separate document/source identities.

    Only closed, checked internal members are linked. Internal REGULAR members
    may have multiple links; every reader still verifies their byte digest.
    """
    root = settings.home / "data/archives/.blobs"
    make_directory(root, settings.home)
    blob = root / digest
    observed = hash_file(source)
    if observed != (digest, size):
        raise RuntimeError("closed member changed before storage")
    source.chmod(0o444)
    try:
        os.link(source, blob)
    except FileExistsError:
        if blob.is_symlink() or hash_file(blob) != (digest, size):
            raise RuntimeError("existing immutable blob is invalid")
    os.link(blob, destination)  # EEXIST is an error, never an overwrite.


def assemble_archive(
    settings: Settings, destination: Path, members: dict[str, Path],
    *, derived: dict[str, Path] | None = None,
) -> dict:
    make_directory(destination, settings.home, exclusive=True)
    files = []
    for name, source in sorted(members.items()):
        relative_path(name)
        digest, size = hash_file(source)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        inside(target.parent, settings.home)
        link_blob(settings, source, target, digest, size)
        files.append({"path": name, "size_bytes": size, "sha256": digest})
    # Multi-member local originals bind the complete file/image closure.
    # Keep legacy and web manifests readable without changing their identity.
    version = 2 if len(files) > 1 and "captured.html" not in members else 1
    manifest = {"schema_version": version, "files": files}
    manifest_path = destination / "archive-manifest.json"
    write_closed(manifest_path, json_bytes(manifest))
    manifest_path.chmod(0o444)
    derived_files = []
    for name, source in sorted((derived or {}).items()):
        if name not in {"local.html", "node-map.json"}:
            raise ValueError("unknown derived archive member")
        target = destination / name
        digest, size = hash_file(source)
        copy_closed(source, target, digest, 20 * 1024 * 1024)
        target.chmod(0o444)
        derived_files.append({"path": name, "size_bytes": size, "sha256": digest})
    if derived_files:
        processing_manifest = destination / "derived-manifest.json"
        write_closed(processing_manifest, json_bytes({
            "schema_version": 1, "raw_manifest_sha256": hash_file(manifest_path)[0],
            "files": derived_files,
        }))
        processing_manifest.chmod(0o444)
    for folder, directories, _files in os.walk(destination, topdown=False, followlinks=False):
        if any((Path(folder) / name).is_symlink() for name in directories):
            raise RuntimeError("archive contains a symlink directory")
        Path(folder).chmod(0o555)
    return verify_archive(manifest_path)


def read_json_closed(path: Path, limit: int = 1024 * 1024) -> tuple[bytes, object]:
    observed = hash_file(path, limit)
    raw = path.read_bytes()
    if len(raw) > limit or (hashlib.sha256(raw).hexdigest(), len(raw)) != observed or hash_file(path, limit) != observed:
        raise ValueError("JSON member changed or exceeded its bound")
    value = json.loads(raw)
    if json_bytes(value) != raw:
        raise ValueError("JSON member is not canonical")
    return raw, value


def parse_captured_html(content: bytes | str):
    """Keep picture source void in lxml's HTML4 view; archived raw is unchanged."""
    from lxml import html
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    offsets, cursor = [], 0
    for line in text.split("\n"):
        offsets.append(cursor)
        cursor += len(line) + 1

    class PictureSourcePositions(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.positions = []

        def handle_starttag(self, tag, attrs):
            if tag == "source":
                line, column = self.getpos()
                self.positions.append(offsets[line - 1] + column + len(self.get_starttag_text()))

        def handle_startendtag(self, tag, attrs):
            pass

    parser = PictureSourcePositions()
    parser.feed(text)
    parser.close()
    parts, cursor = [], 0
    for position in parser.positions:
        parts.extend((text[cursor:position], "</source>"))
        cursor = position
    parts.append(text[cursor:])
    return html.document_fromstring("".join(parts))

def verify_snapshot_image(node, binding: dict, base_url: str) -> None:
    """Schema 2 binds raw attributes and the same-evaluation image selection."""
    keys = {"node_path", "attribute", "source_url", "selected_url", "srcset",
            "current_src", "picture_sources", "local_path"}
    if not isinstance(binding, dict) or set(binding) != keys or binding["attribute"] != "src":
        raise ValueError("invalid snapshot image binding")
    if node.tag != "img" or node.get("src") != binding["source_url"] or node.get("srcset") != binding["srcset"]:
        raise ValueError("captured image attributes differ from the DOM observation")
    if any(binding[key] is not None and not isinstance(binding[key], str) for key in ("source_url", "srcset")):
        raise ValueError("raw image attributes must retain null or string")
    observed_sources = binding["picture_sources"]
    if not isinstance(observed_sources, list):
        raise ValueError("invalid picture source observation")
    source_keys = ("srcset", "media", "type", "sizes")
    if any(not isinstance(item, dict) or set(item) != set(source_keys)
           or any(item[key] is not None and not isinstance(item[key], str) for key in source_keys)
           for item in observed_sources):
        raise ValueError("invalid picture source attributes")
    parent = node.getparent()
    sources = [{key: child.get(key) for key in source_keys} for child in parent
               if child.tag == "source"] if parent is not None and parent.tag == "picture" else []
    if sources != observed_sources:
        raise ValueError("captured picture source order or attributes differ")
    current, selected = binding["current_src"], binding["selected_url"]
    if not isinstance(current, str) or not isinstance(selected, str) or not selected:
        raise ValueError("invalid browser image selection")
    if current:
        if selected != current:
            raise ValueError("selected image differs from captured currentSrc")
    else:
        if not binding["source_url"] or binding["srcset"] is not None or any(item["srcset"] is not None for item in sources):
            raise ValueError("responsive image selection was not ready at capture")
        if selected != urljoin(base_url, binding["source_url"]):
            raise ValueError("fallback image differs from the captured source")

def verify_derived(root: Path, raw_manifest: dict, raw_digest: str) -> dict:
    """Processing bytes are bound separately from the raw archive fingerprint."""
    path = root / "derived-manifest.json"
    raw, record = read_json_closed(path)
    if not isinstance(record, dict) or set(record) != {"schema_version", "raw_manifest_sha256", "files"}:
        raise ValueError("invalid derived manifest")
    if record["schema_version"] != 1 or record["raw_manifest_sha256"] != raw_digest:
        raise ValueError("derived output refers to a different raw manifest")
    files = record["files"]
    if not isinstance(files, list) or [item.get("path") for item in files] != ["local.html", "node-map.json"]:
        raise ValueError("derived member set is incomplete")
    for item in files:
        if set(item) != {"path", "size_bytes", "sha256"} or hash_file(root / item["path"], 20 * 1024 * 1024) != (item["sha256"], item["size_bytes"]):
            raise ValueError("derived bytes changed")
    hashes = {item["path"]: item["sha256"] for item in raw_manifest["files"] + files}
    _node_raw, node_map = read_json_closed(root / "node-map.json")
    if not isinstance(node_map, dict) or set(node_map) != {"schema_version", "nodes", "captured_sha256", "local_sha256"}:
        raise ValueError("invalid node map schema")
    if node_map["schema_version"] != 1 or node_map["captured_sha256"] != hashes["captured.html"] or node_map["local_sha256"] != hashes["local.html"]:
        raise ValueError("captured/local content binding differs")
    _resource_raw, resources = read_json_closed(root / "resource-map.json")
    if not isinstance(resources, dict) or resources.get("schema_version") not in {1, 2}:
        raise ValueError("invalid resource map")
    version = resources["schema_version"]
    expected_keys = {"schema_version", "bindings"} if version == 1 else {"schema_version", "bindings", "base_url", "snapshot_method"}
    if set(resources) != expected_keys:
        raise ValueError("invalid resource map keys")
    if version == 2 and (resources["snapshot_method"] != "SINGLE_SYNCHRONOUS_DOM_EVALUATION"
                         or not isinstance(resources["base_url"], str) or not resources["base_url"].startswith(("http://", "https://"))):
        raise ValueError("invalid snapshot resource identity")
    nodes, bindings = node_map["nodes"], resources["bindings"]
    if not isinstance(nodes, list) or not isinstance(bindings, list) or len(nodes) != len(bindings) or len(nodes) > 100:
        raise ValueError("resource/node count differs")
    from lxml import html
    parse = parse_captured_html if version == 2 else html.document_fromstring
    captured = parse((root / "captured.html").read_bytes())
    local = parse((root / "local.html").read_bytes())
    for node, binding in zip(nodes, bindings):
        if set(node) != {"captured_xpath", "local_xpath", "local_path"}:
            raise ValueError("invalid node mapping")
        if node["captured_xpath"] != binding["node_path"] or node["local_path"] != binding["local_path"] or node["local_path"] not in hashes:
            raise ValueError("resource mapping differs")
        source_nodes, local_nodes = captured.xpath(node["captured_xpath"]), local.xpath(node["local_xpath"])
        if len(source_nodes) != 1 or len(local_nodes) != 1 or source_nodes[0].tag != "img" or local_nodes[0].tag != "img" or local_nodes[0].get("src") != node["local_path"]:
            raise ValueError("node mapping no longer locates the bound image")
        if version == 2:
            verify_snapshot_image(source_nodes[0], binding, resources["base_url"])
            if local_nodes[0].get("srcset") is not None or local_nodes[0].get("data-src") is not None:
                raise ValueError("derived image retained a remote selection attribute")
    if version == 2 and local.xpath("//base|//source"):
        raise ValueError("derived HTML retained a remote base or picture source")
    return {"manifest_path": str(path), "manifest_sha256": hashlib.sha256(raw).hexdigest(), **record}

def verify_archive(manifest_path: Path) -> dict:
    if manifest_path.is_symlink():
        raise ValueError("archive manifest cannot be a symlink")
    raw, manifest = read_json_closed(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files"}:
        raise ValueError("invalid archive manifest keys")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] not in {1, 2} or not isinstance(manifest["files"], list) or not manifest["files"] or len(manifest["files"]) > 102:
        raise ValueError("invalid archive manifest shape")
    if json_bytes(manifest) != raw:
        raise ValueError("archive manifest is not canonical UTF-8 JSON")
    names, total = [], 0
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise ValueError("invalid archive member schema")
        name = relative_path(item["path"])
        if type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
            raise ValueError("invalid archive size")
        digest = item["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid archive SHA-256")
        path = inside(manifest_path.parent / name, manifest_path.parent)
        if hash_file(path) != (digest, item["size_bytes"]):
            raise ValueError("archive member is missing or changed")
        names.append(name)
        total += item["size_bytes"]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("archive members are not sorted and unique")
    actual = set()
    for folder, directories, filenames in os.walk(manifest_path.parent, followlinks=False):
        for name in directories:
            entry = Path(folder) / name
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("archive DIRECTORY branch rejected")
        for name in filenames:
            entry = Path(folder) / name
            if entry.is_symlink() or not entry.is_file():
                raise ValueError("archive non-REGULAR branch rejected")
            actual.add(entry.relative_to(manifest_path.parent).as_posix())
    extras = {"archive-manifest.json"}
    if "captured.html" in names:
        extras.update({"local.html", "node-map.json", "derived-manifest.json"})
    if actual != set(names) | extras:
        raise ValueError("archive members or derived files are incomplete/unexpected")
    digest = hashlib.sha256(raw).hexdigest()
    derived_record = verify_derived(manifest_path.parent, manifest, digest) if "captured.html" in names else None
    content_digest = digest if "captured.html" in names or manifest["schema_version"] == 2 else manifest["files"][0]["sha256"]
    return {
        "manifest_path": str(manifest_path), "manifest_sha256": digest,
        "content_sha256": content_digest, "files": manifest["files"], "size_bytes": total,
        "derived": derived_record,
    }


def local_members(payload: dict, stage_dir: Path) -> tuple[dict[str, Path], dict, dict[str, Path]]:
    metadata = payload["metadata"]
    suffix = Path(metadata["filename"]).suffix.lower()
    name = "original" + suffix
    source = Path(payload["upload_path"])
    target = stage_dir / name
    copy_closed(source, target, metadata["sha256"], FILE_LIMIT)
    members, resource_map = {name: target}, []
    for asset in payload.get("local_asset_uploads", []):
        member = relative_path(asset["archive_member"])
        destination = stage_dir / member
        if member not in members:
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_closed(Path(asset["upload_path"]), destination, asset["sha256"], FILE_LIMIT)
            members[member] = destination
        resource_map.append({key: asset[key] for key in ("relative_path", "sha256", "size_bytes", "archive_member")})
    if resource_map:
        mapping_path = stage_dir / "obsidian-resources.json"
        write_closed(mapping_path, json_bytes({
            "schema_version": 1, "resources": resource_map,
            "image_relations": payload["directory_context"]["image_relations"],
        }))
        members["obsidian-resources.json"] = mapping_path
    return members, {
        "input_kind": "LOCAL_FILE",
        "filename": metadata["filename"],
        "relative_path": metadata.get("relative_path"),
        "provenance": metadata["provenance"],
        "directory_context": payload.get("directory_context"),
        "local_asset_members": resource_map,
        "format_validation": "PENDING_FORMAT_PARSER",
    }, {}
