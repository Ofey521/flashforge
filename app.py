import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from anki.collection import Collection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anki-creator")

DATA_DIR = Path(os.environ.get("ANKI_DATA_DIR", "/data"))
COLLECTION_PATH = DATA_DIR / "collection.anki2"
ANKIWEB_USERNAME = os.environ["ANKIWEB_USERNAME"]
ANKIWEB_PASSWORD = os.environ["ANKIWEB_PASSWORD"]
ANKIWEB_ENDPOINT: Optional[str] = os.environ.get("ANKIWEB_ENDPOINT") or None
API_KEY = os.environ["API_KEY"]

# Anki collection allows only one open handle at a time. Serialize all access.
_lock = threading.Lock()

app = FastAPI(title="anki-creator", version="0.1.0")


def auth_check(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@contextmanager
def open_collection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        col = Collection(str(COLLECTION_PATH))
        try:
            yield col
        finally:
            col.close()


class NoteIn(BaseModel):
    deck: str
    model: str
    fields: Dict[str, str]
    tags: List[str] = []


class AddNotesRequest(BaseModel):
    notes: List[NoteIn]
    allow_duplicate: bool = False


class CanAddRequest(BaseModel):
    notes: List[NoteIn]


@app.get("/healthz")
def healthz():
    try:
        with open_collection() as col:
            col.db.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:
        log.error("healthz failed: %s", e)
        raise HTTPException(status_code=503, detail="collection not accessible")


@app.get("/decks", dependencies=[Depends(auth_check)])
def list_decks():
    with open_collection() as col:
        decks = [d.name for d in col.decks.all_names_and_ids()]
        log.info("list_decks: %d decks", len(decks))
        return {"decks": decks}


@app.get("/models", dependencies=[Depends(auth_check)])
def list_models():
    with open_collection() as col:
        models = [m.name for m in col.models.all_names_and_ids()]
        log.info("list_models: %d models", len(models))
        return {"models": models}


@app.get("/models/{name}/fields", dependencies=[Depends(auth_check)])
def model_fields(name: str):
    with open_collection() as col:
        nt = col.models.by_name(name)
        if nt is None:
            raise HTTPException(404, f"model not found: {name}")
        return {"fields": col.models.field_names(nt)}


def _build_note(col: Collection, n: NoteIn):
    nt = col.models.by_name(n.model)
    if nt is None:
        raise HTTPException(400, f"unknown model: {n.model}")
    note = col.new_note(nt)
    valid_fields = set(col.models.field_names(nt))
    for field, value in n.fields.items():
        if field not in valid_fields:
            raise HTTPException(400, f"model '{n.model}' has no field '{field}'")
        note[field] = value
    note.tags = list(n.tags)
    return nt, note


@app.post("/can_add", dependencies=[Depends(auth_check)])
def can_add(req: CanAddRequest):
    results = []
    with open_collection() as col:
        for n in req.notes:
            try:
                _nt, note = _build_note(col, n)
            except HTTPException as e:
                results.append({"can_add": False, "reason": e.detail})
                continue
            status = note.duplicate_or_empty()
            # 0 = OK, 1 = empty, 2 = duplicate
            if status == 0:
                results.append({"can_add": True, "reason": None})
            elif status == 1:
                results.append({"can_add": False, "reason": "empty_first_field"})
            else:
                results.append({"can_add": False, "reason": "duplicate"})
    log.info("can_add: %d checked", len(results))
    return {"results": results}


@app.post("/add_notes", dependencies=[Depends(auth_check)])
def add_notes(req: AddNotesRequest):
    added: List[int] = []
    skipped: List[Dict] = []
    with open_collection() as col:
        for n in req.notes:
            _nt, note = _build_note(col, n)
            if not req.allow_duplicate:
                status = note.duplicate_or_empty()
                if status != 0:
                    reason = "empty_first_field" if status == 1 else "duplicate"
                    skipped.append({"fields": n.fields, "reason": reason})
                    continue
            deck_id = col.decks.id(n.deck)
            col.add_note(note, deck_id)
            added.append(note.id)
    log.info("add_notes: added=%d skipped=%d", len(added), len(skipped))
    return {"added": added, "skipped": skipped}


class FindNotesRequest(BaseModel):
    query: str  # Anki search syntax, e.g. "deck:zbocznica::n8n Przód:protect*"


@app.post("/find_notes", dependencies=[Depends(auth_check)])
def find_notes(req: FindNotesRequest):
    with open_collection() as col:
        note_ids = col.find_notes(req.query)
        notes = []
        for nid in note_ids:
            note = col.get_note(nid)
            fields = {name: note[name] for name in note.keys()}
            notes.append({"id": nid, "fields": fields, "tags": list(note.tags)})
        log.info("find_notes: query=%r found=%d", req.query, len(notes))
        return {"count": len(notes), "notes": notes}


class SyncRequest(BaseModel):
    force_direction: Optional[str] = None  # "upload" | "download" | null


@app.post("/sync", dependencies=[Depends(auth_check)])
def sync(req: SyncRequest = SyncRequest()):
    """Sync z AnkiWeb. Obsługuje pierwszy bootstrap (FULL_DOWNLOAD)."""
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        col = Collection(str(COLLECTION_PATH))
        try:
            auth = col.sync_login(
                username=ANKIWEB_USERNAME,
                password=ANKIWEB_PASSWORD,
                endpoint=ANKIWEB_ENDPOINT,
            )
            if auth.endpoint:
                log.info("sync using endpoint=%s", auth.endpoint)

            result = col.sync_collection(auth, sync_media=False)
            if result.new_endpoint:
                auth.endpoint = result.new_endpoint
                log.info("sync redirected to endpoint=%s", auth.endpoint)

            # SyncCollectionResponse.required enum:
            #   NO_CHANGES=0, NORMAL_SYNC=1, FULL_SYNC=2, FULL_DOWNLOAD=3, FULL_UPLOAD=4
            # NOTE: sync_collection already performs normal sync during the call.
            #   required=0 means "completed, no full sync needed" (changes may have synced).
            #   required=1 means "normal sync done".
            #   required=2,3,4 means full sync still needed — caller must run it.
            required = result.required
            log.info("sync result.required=%s", required)

            if required in (2, 3, 4):
                # Full sync — close collection first, then re-open for transfer
                if req.force_direction == "upload":
                    upload = True
                elif req.force_direction == "download":
                    upload = False
                else:
                    upload = (required == 4)  # FULL_UPLOAD=4
                server_usn = result.server_media_usn
                col.close(downgrade=False)
                col = Collection(str(COLLECTION_PATH))
                col.full_upload_or_download(
                    auth=auth, server_usn=server_usn, upload=upload
                )
                status = "full_upload" if upload else "full_download"
                return {"status": status}
            # required=0 or 1: sync already completed by sync_collection
            return {"status": "synced"}
        finally:
            col.close()
