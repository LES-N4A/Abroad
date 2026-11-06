"""
api.py - the DOOR between the website and the brain (engine.py).
The browser (static/index.html) sends HTTP requests like  POST /api/orders/local ;
FastAPI receives them here, calls the matching function in engine.py and replies with JSON.
Run:  python -m uvicorn api:app --reload   then open http://127.0.0.1:8000
"""
import base64, os, re, threading
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import currency
from engine import Abroad, EscrowError, TND

app = FastAPI(title="Abroad")
core = Abroad(os.environ.get("ABROAD_DB", "abroad_v2.db"))   # ONE engine shared by all requests
lock = threading.Lock()          # requests run in parallel; the lock makes them queue so the database stays consistent
INDEX = Path(__file__).parent / "static" / "index.html"
MAX_PHOTO_BYTES = 700_000

def guard(fn, *a):
    """Run an engine function safely: if a rule is broken (EscrowError) reply with HTTP 400 + the message."""
    try:
        with lock: return fn(*a)
    except EscrowError as e: raise HTTPException(400, str(e))

# --- Request shapes. Pydantic checks that incoming JSON has these fields and types, else it rejects it.
class Login(BaseModel): name: str
class ListingIn(BaseModel): seller_id: int; title: str; price_tnd: float; image: Optional[str] = None   # image = data URL
class TripIn(BaseModel): traveler_id: int; country: str; kg: float; date: str
class LocalOrder(BaseModel): buyer_id: int; listing_id: int
class Item(BaseModel): name: str; url: str = ""; price: float; currency: str = "TND"; qty: int = 1; weight_kg: float = 0.5
class QuoteIn(BaseModel): trip_id: int; items: List[Item]
class TripOrder(BaseModel): buyer_id: int; trip_id: int; items: List[Item]
class Pay(BaseModel): gateway_ref: str
class Ship(BaseModel): provider_id: int
class Code(BaseModel): code: str
class Rate(BaseModel): stars: int
class Resolve(BaseModel): in_favor_of: str

def m(x):  # website sends TND (120.5); engine wants millimes (120500)
    return int(round(x * TND))

def decode_photo(data_url):
    """The browser sends the photo as text ("data:image/jpeg;base64,...."). Turn it back into bytes."""
    found = re.match(r"^data:image/(jpeg|png|webp);base64,(.+)$", data_url, re.S)
    if not found: raise HTTPException(400, "Photo must be a JPG, PNG or WebP image")
    try: raw = base64.b64decode(found.group(2), validate=True)
    except Exception: raise HTTPException(400, "Photo must be a JPG, PNG or WebP image")
    if len(raw) > MAX_PHOTO_BYTES: raise HTTPException(400, "Photo is too large (max 700 KB)")
    return raw, "image/" + found.group(1)

# --- Routes: a decorator like @app.post(...) links a URL to the function right below it.
@app.get("/", include_in_schema=False)
def home(): return FileResponse(INDEX, headers={"Cache-Control": "no-store"})   # never reuse an old copy

@app.post("/api/login")
def login(b: Login): return guard(core.login, b.name)
@app.get("/api/me/{uid}")
def me(uid: int): return guard(core.user, uid)

@app.get("/api/listings")
def listings(): return guard(core.list_listings)
@app.post("/api/listings")
def add_listing(b: ListingIn):
    if b.price_tnd <= 0 or not b.title.strip(): raise HTTPException(400, "Enter a title and a price")
    raw, mime = decode_photo(b.image) if b.image else (None, None)
    return {"id": guard(core.add_listing, b.seller_id, b.title.strip()[:80], m(b.price_tnd), raw, mime)}
@app.get("/api/listings/{lid}/image")
def listing_image(lid: int):
    found = guard(core.get_image, lid)
    if not found: raise HTTPException(404, "no photo")
    return Response(content=found[0], media_type=found[1],
                    headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

@app.get("/api/trips")
def trips(): return guard(core.list_trips)
@app.post("/api/trips")
def add_trip(b: TripIn): return {"id": guard(core.add_trip, b.traveler_id, b.country, b.kg, b.date)}

@app.get("/api/rates")
def rates(): return guard(currency.get_rates)
@app.post("/api/quote/items")
def quote(b: QuoteIn): return guard(core.quote_trip_items, b.trip_id, [dict(i) for i in b.items])

@app.post("/api/orders/local")
def local(b: LocalOrder): return {"id": guard(core.create_local_order, b.buyer_id, b.listing_id)}
@app.post("/api/orders/traveler")
def traveler(b: TripOrder): return {"id": guard(core.create_trip_order, b.buyer_id, b.trip_id, [dict(i) for i in b.items])}
@app.get("/api/users/{uid}/orders")
def orders(uid: int): return guard(core.list_orders, uid)
@app.get("/api/orders/{oid}")
def order(oid: int): return guard(core.get, oid)
@app.post("/api/orders/{oid}/pay")
def pay(oid: int, b: Pay): guard(core.fund, oid, b.gateway_ref); return {"ok": True}
@app.post("/api/orders/{oid}/ship")
def ship(oid: int, b: Ship): guard(core.ship, oid, b.provider_id); return {"ok": True}
@app.post("/api/orders/{oid}/confirm")
def confirm(oid: int, b: Code): guard(core.confirm_delivery, oid, b.code); return {"ok": True}
@app.post("/api/orders/{oid}/dispute")
def dispute(oid: int): guard(core.dispute, oid); return {"ok": True}
@app.post("/api/orders/{oid}/rate")
def rate(oid: int, b: Rate): guard(core.rate, oid, b.stars); return {"ok": True}
@app.get("/api/admin/disputes")
def disputes(): return guard(core.list_orders, 0, "DISPUTED")
@app.post("/api/orders/{oid}/resolve")   # add real admin authentication before going live
def resolve(oid: int, b: Resolve): guard(core.resolve, oid, b.in_favor_of); return {"ok": True}
