"""Explicit MOCK browser contracts and bounded, immutable static build bytes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping
from xml.etree import ElementTree

from starlette.requests import Request
from starlette.responses import Response

from api.beta_oidc import ORIGIN, SESSION_ABSOLUTE, SESSION_IDLE, Session
from src.interop.epic.beta_catalog import MOCK_PATIENTS, Catalog, RefreshRejected

SESSION_ROUTE = "/beta/auth/session"
PATIENTS_ROUTE = "/beta/epic/patients"
BROWSER_LOGIN = "/beta/auth/browser-login"
SHELL = "/beta/app/"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self' https://owner-idp.invalid; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
)
READ_PROFILE = {
    "x-beta-client": "browser-v1",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "same-origin",
    "sec-fetch-dest": "empty",
    "accept": "application/json",
}
FORM_PROFILE = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
FILE_LIMIT = 4 * 1024 * 1024
TOTAL_LIMIT = 16 * 1024 * 1024
COUNT_LIMIT = 128
MANIFEST_LIMIT = 64 * 1024
ASSET_PATH = re.compile(r"assets/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
MIME = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}
LABELS = {"mock-alpha": "MOCK Alpha", "mock-bravo": "MOCK Bravo"}


def static_namespace(path: str) -> bool:
    return path == SHELL[:-1] or path.startswith(SHELL)


def browser_path(path: str) -> bool:
    return static_namespace(path) or path in {SESSION_ROUTE, PATIENTS_ROUTE, BROWSER_LOGIN}


def _one(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else ""


def payload_length(request: Request) -> None:
    values = request.headers.getlist("content-length")
    if values and (len(values) != 1 or not values[0] or values[0].strip("0")):
        raise RefreshRejected(400, "unexpected_payload")


def read_profile(request: Request) -> None:
    if any(_one(request, name) != value for name, value in READ_PROFILE.items()):
        raise RefreshRejected(403, "same_origin_required")
    origin = request.headers.getlist("origin")
    if origin and origin != [ORIGIN]:
        raise RefreshRejected(403, "same_origin_required")


def form_profile(request: Request) -> None:
    if _one(request, "origin") != ORIGIN:
        raise RefreshRejected(403, "invalid_origin")
    accepts = request.headers.getlist("accept")
    if any(_one(request, name) != value for name, value in FORM_PROFILE.items()) or len(accepts) != 1:
        raise RefreshRejected(403, "same_origin_required")
    if "text/html" not in {part.split(";", 1)[0].strip().lower() for part in accepts[0].split(",")}:
        raise RefreshRejected(403, "same_origin_required")


def session_view(session: Session, now: float) -> dict[str, Any]:
    return {
        "authenticated": True,
        "mode": "MOCK",
        "owner_label": "Authenticated MOCK owner",
        "csrf_token": session.csrf,
        "server_time_unix_s": now,
        "created_at_unix_s": session.created,
        "last_activity_at_unix_s": session.used,
        "idle_expires_at_unix_s": session.used + SESSION_IDLE,
        "absolute_expires_at_unix_s": session.created + SESSION_ABSOLUTE,
    }


def patient_view(catalog: Catalog) -> dict[str, Any]:
    return {
        "mode": "MOCK",
        "source": "epic-sandbox-mock",
        "patients": [
            {"alias": alias, "label": LABELS[alias]} for alias in sorted(MOCK_PATIENTS) if not catalog.revoked(alias)
        ],
    }


def _plain(path: Path, *, directory: bool) -> None:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 1024:
        raise ValueError
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
        raise ValueError


def _read(root: Path, relative: str, limit: int) -> bytes:
    path = root / relative
    for parent in reversed(path.parent.parents):
        _plain(parent, directory=True)
    _plain(path.parent, directory=True)
    _plain(path, directory=False)
    if not path.resolve().is_relative_to(root):
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _plain(path, directory=False)
    current = path.stat()

    def signature(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if len(data) > limit or signature(before) != signature(after) or signature(after) != signature(current):
        raise ValueError
    if not path.resolve().is_relative_to(root):
        raise ValueError
    return data


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError


def _asset_name(value: Any) -> str:
    if not isinstance(value, str) or not ASSET_PATH.fullmatch(value) or Path(value).suffix.lower() not in MIME:
        raise ValueError
    return value


def _local_reference(value: str, paths: set[str]) -> None:
    if value.startswith("#"):
        return
    name = value.removeprefix(SHELL) if value.startswith(SHELL) else "assets/" + value.removeprefix("./")
    if name not in paths:
        raise ValueError


def _css(text: str, paths: set[str]) -> None:
    for match in re.finditer(r"url\(\s*([^)]*)\)|@import\s+([\"'][^\"']*[\"'])", text, re.IGNORECASE):
        _local_reference((match[1] or match[2]).strip().strip("\"'"), paths)


class _ShellParser(HTMLParser):
    def __init__(self, paths: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.paths = paths
        self.script = False
        self.modules = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"base", "iframe", "object", "embed", "style", "form"}:
            raise ValueError
        values = dict(attrs)
        if len(values) != len(attrs) or any(name.startswith("on") for name in values):
            raise ValueError
        if "srcset" in values or "srcdoc" in values:
            raise ValueError
        if tag == "meta" and "http-equiv" in values:
            raise ValueError
        for name in ("src", "href", "xlink:href", "poster"):
            if name in values:
                _local_reference(values[name] or "", self.paths)
        if "style" in values:
            _css(values["style"] or "", self.paths)
        if tag == "script":
            if self.script or values.get("type") != "module" or not (values.get("src") or "").endswith(".js"):
                raise ValueError
            self.script = True
            self.modules += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.script = False

    def handle_data(self, data: str) -> None:
        if self.script and data.strip():
            raise ValueError


def _content(name: str, data: bytes, paths: set[str]) -> None:
    suffix = Path(name).suffix.lower()
    if suffix in {".js", ".css", ".svg", ".html"}:
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError
        if suffix == ".html":
            parser = _ShellParser(paths)
            parser.feed(text)
            parser.close()
            if parser.script or not parser.modules:
                raise ValueError
        elif suffix == ".css":
            _css(text, paths)
        elif suffix == ".js":
            if re.search(r"(?:from\s*|import\s*(?:\(\s*)?)[\"'](?:https?:|//|data:)", text, re.IGNORECASE):
                raise ValueError
            if re.search(r"serviceWorker\s*\.\s*register|sourceMappingURL\s*=", text):
                raise ValueError
        elif re.search(
            r"<!DOCTYPE|<!ENTITY|<\s*(?:script|foreignObject)\b|\bon\w+\s*=|(?:href|src)\s*=\s*[\"'](?!#)",
            text,
            re.IGNORECASE,
        ):
            raise ValueError
        elif suffix == ".svg":
            if re.search(r"<\?xml-stylesheet", text, re.IGNORECASE):
                raise ValueError
            image = ElementTree.fromstring(text)
            if image.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
                raise ValueError
            for element in image.iter():
                for key, value in element.attrib.items():
                    local = key.rsplit("}", 1)[-1].lower()
                    if local.startswith("on") or (local in {"href", "src"} and not value.startswith("#")):
                        raise ValueError
                    if local == "style":
                        _css(value, paths)
        return
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
        ".ico": (b"\x00\x00\x01\x00",),
        ".woff": (b"wOFF",),
        ".woff2": (b"wOF2",),
        ".ttf": (b"\x00\x01\x00\x00",),
        ".otf": (b"OTTO",),
    }
    if suffix == ".webp":
        valid = data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    elif suffix == ".avif":
        valid = data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}
    else:
        valid = data.startswith(signatures.get(suffix, (b"\x00-invalid",)))
    if not valid:
        raise ValueError


@dataclass(frozen=True)
class Asset:
    body: bytes
    media_type: str
    sha256: str


@dataclass(frozen=True)
class BrowserAssets:
    files: Mapping[str, Asset]

    @classmethod
    def load(cls, supplied: Path) -> BrowserAssets:
        try:
            root = supplied.absolute()
            if ".." in root.parts:
                raise ValueError
            for part in [*reversed(root.parents), root]:
                _plain(part, directory=True)
            root = root.resolve()
            manifest = json.loads(
                _read(root, ".vite/manifest.json", MANIFEST_LIMIT),
                object_pairs_hook=_object,
                parse_constant=_invalid_constant,
            )
            if not isinstance(manifest, dict) or not 1 <= len(manifest) <= COUNT_LIMIT:
                raise ValueError
            entry = manifest.get("index.html")
            if (
                not isinstance(entry, dict)
                or entry.get("isEntry") is not True
                or not _asset_name(entry.get("file")).endswith(".js")
            ):
                raise ValueError
            paths: set[str] = set()
            visited: set[str] = set()

            def visit(key: str) -> None:
                if key in visited:
                    return
                visited.add(key)
                item = manifest.get(key)
                if not isinstance(item, dict):
                    raise ValueError
                paths.add(_asset_name(item.get("file")))
                for field in ("css", "assets"):
                    values = item.get(field, [])
                    if not isinstance(values, list):
                        raise ValueError
                    paths.update(_asset_name(value) for value in values)
                for field in ("imports", "dynamicImports"):
                    values = item.get(field, [])
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        raise ValueError
                    for value in values:
                        visit(value)

            visit("index.html")
            if len(paths) + 1 > COUNT_LIMIT:
                raise ValueError
            files: dict[str, Asset] = {}
            total = 0
            for name in ["index.html", *sorted(paths)]:
                data = _read(root, name, FILE_LIMIT)
                total += len(data)
                if total > TOTAL_LIMIT:
                    raise ValueError
                _content(name, data, paths)
                url = SHELL if name == "index.html" else SHELL + name
                files[url] = Asset(
                    data,
                    "text/html" if name == "index.html" else MIME[Path(name).suffix.lower()],
                    hashlib.sha256(data).hexdigest(),
                )
            return cls(MappingProxyType(files))
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ElementTree.ParseError):
            raise ValueError("unsafe browser assets") from None

    def response(self, path: str, *, head: bool) -> Response:
        asset = self.files.get(path)
        if asset is None:
            raise RefreshRejected(404, "not_found")
        return Response(
            b"" if head else asset.body,
            media_type=asset.media_type,
            headers={"Content-Security-Policy": CSP, "Content-Length": str(len(asset.body))},
        )
