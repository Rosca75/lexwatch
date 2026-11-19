"""A fake data.public.lu (udata API + stable resource alias + download host) for tests.

The API answers with the *real* checksum of the bytes it currently serves (adapters verify
it), so editing ``files`` publishes a new version. ``set_resource(..., checksum=...)`` simulates
a re-upload: the served file gets an equivalent but byte-different variant (an extra blank
line for CSV, a zip comment for XLSX) and the API publishes its real checksum with the given
algorithm. ``checksum=None`` publishes no checksum.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures"
DATA = Path(__file__).resolve().parent / "data"
API = "https://data.public.lu/api/1/"

ID_112 = "63c6884fcfa8113ec4793d13"
ID_121 = "67f775f1adf67ff4c46f1b37"
SLUG_109 = "liste-des-votes-en-seances-plenieres"
CSV_112 = "0a85459a-7a17-4d84-bd72-a95347be688d"
XLSX_121 = "8d8bf90f-aba9-4928-98bb-66bbdc3bae6b"
CSV_109 = "d1aba685-67bf-4cce-bdad-140fa11872ac"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class DataPublicMock:
    """Serves dataset JSON and files; records every requested URL in ``calls``."""

    def __init__(self) -> None:
        self.datasets: dict[str, dict] = {
            ID_112: load_json(FIXTURES / "datapublic" / "dataset_112.json"),
            ID_121: load_json(FIXTURES / "datapublic" / "dataset_121.json"),
            SLUG_109: load_json(DATA / "dataset_109.json"),
        }
        self.files: dict[str, bytes] = {
            CSV_112: (FIXTURES / "datapublic" / "112-texte-loi.sample.csv").read_bytes(),
            XLSX_121: (FIXTURES / "datapublic" / "121-etat-travaux.sample.xlsx").read_bytes(),
            CSV_109: (DATA / "109-votes.synthetic.csv").read_bytes(),
        }
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}  # path substring -> HTTP status to return
        self.on_request: Callable[[httpx.Request], httpx.Response | None] | None = None

    def set_resource(self, dataset: str, resource_id: str, **fields: object) -> None:
        for res in self.datasets[dataset]["resources"]:
            if res["id"] == resource_id:
                res.update(copy.deepcopy(fields))
        if fields.get("checksum") and resource_id in self.files:
            self.bump(resource_id)

    def bump(self, resource_id: str) -> None:
        """Replace a served file with an equivalent, byte-different variant (a re-upload)."""
        self._version = getattr(self, "_version", 0) + 1
        data = self.files[resource_id]
        if data[:2] == b"PK":
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, "a") as zf:
                zf.comment = f"lexwatch-test-v{self._version}".encode()
            self.files[resource_id] = buf.getvalue()
        else:
            self.files[resource_id] = data + b"\n"

    def published(self, data: dict) -> dict:
        """Dataset JSON with the real checksums of the files currently served."""
        data = copy.deepcopy(data)
        for res in data.get("resources", []):
            checksum = res.get("checksum")
            if isinstance(checksum, dict) and res.get("id") in self.files:
                algo = checksum.get("type") or "sha1"
                checksum["value"] = hashlib.new(algo, self.files[res["id"]]).hexdigest()
        return data

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for part, status in self.fail.items():
            if part in url:
                return httpx.Response(status, text="boom")
        if self.on_request is not None and (resp := self.on_request(request)) is not None:
            return resp
        path = request.url.path
        if path.startswith("/api/1/datasets/"):
            ref = path.removeprefix("/api/1/datasets/").strip("/")
            for key, data in self.datasets.items():
                if ref in (key, data.get("id"), data.get("slug")):
                    return httpx.Response(200, json=self.published(data))
            return httpx.Response(404, json={"message": "Not found"})
        if path.startswith("/fr/datasets/r/"):
            rid = path.removeprefix("/fr/datasets/r/")
            if rid in self.files:
                return httpx.Response(
                    302, headers={"Location": f"https://download.data.public.lu/x/{rid}"}
                )
            return httpx.Response(404)
        if request.url.host == "download.data.public.lu":
            rid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.files[rid])
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            headers={"User-Agent": "lexwatch-test"},
            follow_redirects=True,
        )

    def downloads(self) -> list[str]:
        return [c for c in self.calls if "download.data.public.lu" in c]
