"""Private per-user portraits; normalize pixels and strip metadata on ingestion."""
import base64
import hashlib
import io
import json
import warnings
import zipfile
from pathlib import Path

from PIL import Image, ImageOps
import repository as repo
import storage

import os
MAX_BYTES=(4 if os.environ.get('VERCEL') else 5)*1024*1024
Image.MAX_IMAGE_PIXELS=16_000_000


def normalize(raw, *, embedded=False):
    if not raw or len(raw)>MAX_BYTES: raise ValueError('Skedari duhet të jetë deri në 4 MB.' if os.environ.get('VERCEL') else 'Fotoja duhet të jetë deri në 5 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                formats = ('JPEG','PNG','WEBP','JPEG2000','TIFF','BMP') if embedded else ('JPEG','PNG','WEBP')
                if source.format not in formats: raise ValueError()
                if source.width<40 or source.height<40: raise ValueError()
                source.load()
                photo=ImageOps.exif_transpose(source).convert('RGB')
                photo.thumbnail((640,640))
                out=io.BytesIO(); photo.save(out,format='JPEG',quality=88)
                return out.getvalue()
    except Exception as exc:
        raise ValueError('Zgjidh një foto të vlefshme JPG, PNG ose WebP.') from exc


def save(raw):
    clean=normalize(raw)
    uid=repo._uid(repo._actor(None))
    digest=hashlib.sha256(clean).hexdigest()[:20]
    key=f'portraits/{uid}/{digest}.jpg'
    store=storage.get_storage(); store.save(key,clean)
    old,_=repo.get_profile_section('avatar')
    repo.set_profile_section('avatar',[{'key':key,'version':digest}])
    if old and old[0].get('key')!=key: store.delete(old[0]['key'])
    return {'ok':True,'url':'/api/profile/photo?v='+digest}


def get():
    entries,_=repo.get_profile_section('avatar')
    if not entries: return None
    try: return storage.get_storage().open(entries[0]['key'])
    except (OSError,ValueError): return None


def delete():
    entries,_=repo.get_profile_section('avatar')
    repo.set_profile_section('avatar',[])
    if entries: storage.get_storage().delete(entries[0]['key'])


def cv_candidates():
    cv=repo.get_cv()
    if not cv: return []
    path=Path(cv['stored_path']); candidates=[]
    def add(raw):
        if len(candidates)>=8: return
        try:
            clean=normalize(raw, embedded=True)
            if clean not in candidates: candidates.append(clean)
        except ValueError: pass
    try:
        raw = storage.read_reference(cv['stored_path'])
        if path.suffix.lower()=='.pdf':
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(raw))
            for page in reader.pages[:4]:
                for n,key in enumerate(page.images.keys()):
                    if n>=12 or len(candidates)>=8: break
                    try: add(page.images[key].data)
                    except Exception: continue
        elif path.suffix.lower()=='.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as doc:
                for item in [i for i in doc.infolist() if i.filename.startswith('word/media/')][:12]:
                    if item.file_size<=MAX_BYTES: add(doc.read(item))
    except Exception:
        return candidates
    return candidates


def previews():
    return [{'index':i,'url':'data:image/jpeg;base64,'+base64.b64encode(raw).decode()} for i,raw in enumerate(cv_candidates())]


def photo_like(raw):
    """Only a layout/tonal heuristic, never face recognition or identity analysis.

    Reject QR codes, flat logos and page scans; ambiguous images stay selectable.
    """
    with Image.open(io.BytesIO(raw)) as img:
        if not .6 <= img.width / img.height <= 1.4:
            return False
        img.thumbnail((96,96))
        histogram = img.convert('L').histogram()
        pixels = sum(histogram)
        return sum(histogram[32:224]) / pixels > .25 and sum(n > 0 for n in histogram) > 64


def autofill(cv_id):
    """Select one unambiguous photo only; preserve existing/manual portraits."""
    cv=repo.get_cv()
    if not cv or cv['id'] != cv_id:
        return {'status':'cv_changed'}
    if repo.get_profile_section('avatar')[0]:
        return {'status':'preserved'}
    candidates=[raw for raw in cv_candidates() if photo_like(raw)]
    if len(candidates) != 1:
        return {'status':'choose' if candidates else 'none', 'count':len(candidates)}
    # Stage the immutable content-addressed object before taking a database write lock.
    # Remote object I/O must not consume an interactive database transaction timeout.
    clean=candidates[0]
    digest=hashlib.sha256(clean).hexdigest()[:20]
    key=f'portraits/{repo._uid(repo._actor(None))}/{digest}.jpg'
    storage.get_storage().save(key,clean)
    # Recheck after extraction/upload before changing the profile's portrait reference.
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        uid=repo._uid(repo._actor(None))
        current=con.execute('SELECT id FROM profile_cv WHERE user_id=?',(uid,)).fetchone()
        avatar=con.execute("SELECT data FROM profile_sections WHERE user_id=? AND section='avatar'",(uid,)).fetchone()
        if not current or current['id'] != cv_id: return {'status':'cv_changed'}
        if avatar and json.loads(avatar['data']): return {'status':'preserved'}
        data=json.dumps([{'key':key,'version':digest,'source':f'cv:{cv_id}'}])
        con.execute("INSERT INTO profile_sections(user_id,profile_id,section,data,updated_at) VALUES (?,1,'avatar',?,?) ON CONFLICT(user_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",(uid,data,repo.now()))
    return {'status':'saved', 'url':'/api/profile/photo?v='+digest}
