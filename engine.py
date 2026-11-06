"""
engine.py - the BRAIN of Abroad. It contains all the business rules:
escrow states, fees, delivery codes, ratings, Pro Seller status.
It knows nothing about the web: api.py calls these functions.
"""
import hashlib, hmac, json, os, secrets, sqlite3, time
import currency
from enum import Enum

# ---------------------------------------------------------------- 1. MONEY
# Floats are inexact in programming (0.1 + 0.2 = 0.30000000000000004), which is
# dangerous for money. So we store whole numbers of MILLIMES: 1 TND = 1000 millimes.
TND = 1000
def fmt(m):                       # 129600 -> "129.600 TND"
    return f"{m / TND:,.3f} TND"

# ---------------------------------------------------------------- 2. STATE MACHINE
# Every order is always in exactly ONE state. An Enum is a fixed list of allowed names.
class S(str, Enum):
    CREATED = "CREATED"      # order made, buyer has not paid yet
    FUNDED = "FUNDED"        # buyer paid, money is LOCKED in escrow
    SHIPPED = "SHIPPED"      # seller/traveler sent the item
    RELEASED = "RELEASED"    # code validated, money paid to seller (final)
    DISPUTED = "DISPUTED"    # buyer reported a problem, admin decides
    REFUNDED = "REFUNDED"    # money returned to buyer (final)
    CANCELLED = "CANCELLED"  # never paid (final)

# The dictionary below IS the state machine: "from this state, you may only go to these".
# Anything else is refused, so nobody can e.g. release money for an unpaid order.
TRANSITIONS = {
    S.CREATED: {S.FUNDED, S.CANCELLED},
    S.FUNDED: {S.SHIPPED, S.REFUNDED},
    S.SHIPPED: {S.RELEASED, S.DISPUTED},
    S.DISPUTED: {S.RELEASED, S.REFUNDED},
    S.RELEASED: set(), S.REFUNDED: set(), S.CANCELLED: set(),   # final: nowhere to go
}

# ---------------------------------------------------------------- 3. SETTINGS
TRAVELER_RATE_PER_KG = {"FR": 15*TND, "IT": 14*TND, "DE": 16*TND,   # reward per kg by country
                        "TR": 12*TND, "AE": 18*TND, "CA": 22*TND}
TRAVELER_FEE_PCT = 10                  # platform keeps 10% of the traveler's reward
MAX_CODE_ATTEMPTS = 5                  # stops people guessing the 6-digit code
PRO_MIN_ORDERS, PRO_MIN_RATING, PRO_MAX_DISPUTE_RATE = 30, 4.6, 0.01   # Pro Seller rules

class EscrowError(Exception):
    """Our own error type, raised when someone breaks a rule (api.py turns it into a message)."""

# ---------------------------------------------------------------- 4. DATABASE TABLES
# SQLite = a database stored in one file. SQL below creates 5 tables (like Excel sheets).
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, role TEXT,
  completed INTEGER DEFAULT 0, disputes INTEGER DEFAULT 0,
  rating_sum REAL DEFAULT 0, rating_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY, seller_id INTEGER, title TEXT, price INTEGER, image BLOB, mime TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, kind TEXT, buyer_id INTEGER,
  provider_id INTEGER, title TEXT, item_price INTEGER, reward INTEGER DEFAULT 0,
  fee INTEGER, total INTEGER, state TEXT, nonce TEXT, code_attempts INTEGER DEFAULT 0,
  rated INTEGER DEFAULT 0, created REAL, items TEXT, listing_id INTEGER);
CREATE TABLE IF NOT EXISTS trips(id INTEGER PRIMARY KEY, traveler_id INTEGER, country TEXT,
  kg_left REAL, date TEXT);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, order_id INTEGER, account TEXT,
  amount INTEGER, note TEXT, ts REAL);          -- every money movement, like a bank statement
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, order_id INTEGER,
  old TEXT, new TEXT, ts REAL);                 -- history of state changes (audit trail)
"""

class Abroad:
    """One object = the whole platform. Create it once: core = Abroad('file.db')."""

    def __init__(self, path=":memory:", secret=None):
        self.db = sqlite3.connect(path, check_same_thread=False)   # open/create the db file
        self.db.row_factory = sqlite3.Row            # lets us read columns by name: row["total"]
        self.db.executescript(SCHEMA)
        # The secret key makes delivery codes impossible to predict. Never share it.
        self.secret = secret or os.environ.get("ABROAD_SECRET", "dev-secret-change-me").encode()
        self._add_missing_columns()

    def _add_missing_columns(self):
        """Older database files lack the newest columns; add them so an upgrade keeps your data."""
        wanted = {"listings": [("image", "BLOB"), ("mime", "TEXT")], "orders": [("items", "TEXT"), ("listing_id", "INTEGER")]}
        for table, cols in wanted.items():
            have = [r["name"] for r in self._q(f"PRAGMA table_info({table})")]
            for name, kind in cols:
                if name not in have: self._q(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()

    # ------------------------------------------------ small internal helpers (start with _)
    def _q(self, sql, *a):                           # run one SQL command; ? are safe placeholders
        return self.db.execute(sql, a)

    def _order(self, oid):                           # fetch one order or fail politely
        r = self._q("SELECT * FROM orders WHERE id=?", oid).fetchone()
        if not r: raise EscrowError("order not found")
        return r

    def code_for(self, oid):
        """The 6-digit delivery code is CALCULATED, not stored: HMAC(secret, order id + random nonce).
        Same inputs always give the same code, so the buyer can see it again at any time,
        but nobody without our secret key can compute it."""
        o = self._order(oid)
        d = hmac.new(self.secret, f"{oid}:{o['nonce']}".encode(), hashlib.sha256).digest()
        return f"{int.from_bytes(d[:4], 'big') % 10**6:06d}"

    def _move(self, oid, new):
        """Change an order's state, ONLY if the state machine allows it."""
        old = S(self._order(oid)["state"])
        if new not in TRANSITIONS[old]:
            raise EscrowError(f"illegal transition {old.value} -> {new.value}")
        self._q("UPDATE orders SET state=? WHERE id=?", new.value, oid)
        self._q("INSERT INTO events(order_id,old,new,ts) VALUES(?,?,?,?)", oid, old.value, new.value, time.time())

    def _ledger(self, oid, account, amount, note):
        """Write a money movement. Accounts: ESCROW, PLATFORM, USER:<id>. Positive = in, negative = out."""
        self._q("INSERT INTO ledger(order_id,account,amount,note,ts) VALUES(?,?,?,?,?)",
                oid, account, amount, note, time.time())

    # ------------------------------------------------ users, ratings, Pro Seller
    def add_user(self, name, role="both"):
        c = self._q("INSERT INTO users(name,role) VALUES(?,?)", name, role)
        self.db.commit()                              # commit = make the change permanent
        return c.lastrowid                            # the new user's id

    def stats(self, uid):
        u = self._q("SELECT * FROM users WHERE id=?", uid).fetchone()
        rating = u["rating_sum"] / u["rating_count"] if u["rating_count"] else 0.0   # average stars
        dispute_rate = u["disputes"] / u["completed"] if u["completed"] else 0.0
        return dict(completed=u["completed"], rating=round(rating, 2), dispute_rate=dispute_rate)

    def is_pro(self, uid):                            # all three conditions must be true
        s = self.stats(uid)
        return (s["completed"] >= PRO_MIN_ORDERS and s["rating"] >= PRO_MIN_RATING
                and s["dispute_rate"] < PRO_MAX_DISPUTE_RATE)

    def local_fee(self, seller_id, price):
        big = price >= 100 * TND
        pct = (5 if big else 6) if self.is_pro(seller_id) else (8 if big else 10)
        return price * pct // 100                     # // = whole-number division (no decimals)

    # ------------------------------------------------ listings and creating orders
    def add_listing(self, seller_id, title, price, image=None, mime=None):   # image = raw photo bytes (optional)
        c = self._q("INSERT INTO listings(seller_id,title,price,image,mime) VALUES(?,?,?,?,?)", seller_id, title, price, image, mime)
        self.db.commit(); return c.lastrowid

    def create_local_order(self, buyer_id, listing_id):
        l = self._q("SELECT * FROM listings WHERE id=?", listing_id).fetchone()
        if not l: raise EscrowError("listing not found")
        fee = self.local_fee(l["seller_id"], l["price"])
        return self._create("LOCAL", buyer_id, l["seller_id"], l["title"], l["price"], 0, fee, listing_id=listing_id)

    def create_traveler_order(self, buyer_id, traveler_id, title, item_price, country, weight_kg):
        if country not in TRAVELER_RATE_PER_KG: raise EscrowError("country not supported")
        reward = int(TRAVELER_RATE_PER_KG[country] * weight_kg)   # traveler's pay = rate x weight
        fee = reward * TRAVELER_FEE_PCT // 100
        return self._create("TRAVELER", buyer_id, traveler_id, title, item_price, reward, fee)

    def _create(self, kind, buyer, provider, title, item_price, reward, fee, items=None, listing_id=None):
        if buyer == provider: raise EscrowError("You can't buy from yourself")
        total = item_price + reward + fee             # what the buyer pays in total
        c = self._q("INSERT INTO orders(kind,buyer_id,provider_id,title,item_price,reward,fee,total,state,created,items,listing_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    kind, buyer, provider, title, item_price, reward, fee, total, S.CREATED.value, time.time(), items, listing_id)
        self.db.commit(); return c.lastrowid

    # ------------------------------------------------ THE ESCROW FLOW (the heart of the project)
    def fund(self, oid, gateway_ref):
        """STEP 1 - buyer's payment confirmed: lock the money in escrow."""
        o = self._order(oid)
        self._move(oid, S.FUNDED)
        self._q("UPDATE orders SET nonce=? WHERE id=?", secrets.token_hex(8), oid)   # random seed for the code
        self._ledger(oid, "ESCROW", o["total"], f"buyer payment {gateway_ref}")      # money IN to escrow
        self.db.commit()
        return self.code_for(oid)

    def ship(self, oid, provider_id):
        """STEP 2 - only the seller/traveler of THIS order can mark it shipped."""
        if self._order(oid)["provider_id"] != provider_id: raise EscrowError("not the provider")
        self._move(oid, S.SHIPPED); self.db.commit()

    def confirm_delivery(self, oid, code):
        """STEP 3 - the right code releases the money. Wrong codes are counted and limited."""
        o = self._order(oid)
        if S(o["state"]) != S.SHIPPED: raise EscrowError("order is not in SHIPPED state")
        if o["code_attempts"] >= MAX_CODE_ATTEMPTS:
            raise EscrowError("too many attempts, order locked: open a dispute")
        # compare_digest compares in constant time, which blocks "timing attacks"
        if not hmac.compare_digest(self.code_for(oid), code.strip()):
            self._q("UPDATE orders SET code_attempts=code_attempts+1 WHERE id=?", oid); self.db.commit()
            raise EscrowError("Wrong code. Please check it and try again.")
        self._release(oid); self.db.commit()

    def _release(self, oid):
        """Split the escrow money: provider gets total - fee, platform keeps the fee."""
        o = self._order(oid); self._move(oid, S.RELEASED)
        self._ledger(oid, "ESCROW", -o["total"], "release")                       # money OUT of escrow
        self._ledger(oid, f"USER:{o['provider_id']}", o["total"] - o["fee"], "payout")
        self._ledger(oid, "PLATFORM", o["fee"], "fee")
        self._q("UPDATE users SET completed=completed+1 WHERE id=?", o["provider_id"])

    def rate(self, oid, stars):
        o = self._order(oid)
        if S(o["state"]) != S.RELEASED: raise EscrowError("can only rate completed orders")
        if not 1 <= stars <= 5: raise EscrowError("stars must be 1-5")
        if o["rated"]: raise EscrowError("already rated")
        self._q("UPDATE users SET rating_sum=rating_sum+?, rating_count=rating_count+1 WHERE id=?",
                stars, o["provider_id"])
        self._q("UPDATE orders SET rated=1 WHERE id=?", oid); self.db.commit()

    def dispute(self, oid):                           # buyer reports a problem; counts against the provider
        o = self._order(oid); self._move(oid, S.DISPUTED)
        self._q("UPDATE users SET disputes=disputes+1 WHERE id=?", o["provider_id"]); self.db.commit()

    def resolve(self, oid, in_favor_of):              # admin decides: "provider" gets paid, else buyer is refunded
        if in_favor_of == "provider": self._release(oid)
        else: self._refund(oid)
        self.db.commit()

    def refund_unshipped(self, oid):                  # provider never shipped -> give the money back
        self._refund(oid); self.db.commit()

    def _refund(self, oid):
        o = self._order(oid); self._move(oid, S.REFUNDED)
        self._ledger(oid, "ESCROW", -o["total"], "refund")
        self._ledger(oid, f"USER:{o['buyer_id']}", o["total"], "refund to buyer")

    def balance(self, account):                       # a balance = the sum of all ledger lines
        return self._q("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE account=?", account).fetchone()["s"]

    def get(self, oid):
        o = dict(self._order(oid)); o.pop("nonce"); return o      # never expose the nonce

    # ------------------------------------------------ helpers used by the website
    def login(self, name):                            # find the user by name, or create them
        name = name.strip()
        if not 2 <= len(name) <= 40: raise EscrowError("Please enter your name (2-40 characters)")
        r = self._q("SELECT id FROM users WHERE lower(name)=lower(?)", name).fetchone()
        return self.user(r["id"] if r else self.add_user(name))

    def user(self, uid):
        u = self._q("SELECT id,name FROM users WHERE id=?", uid).fetchone()
        if not u: raise EscrowError("user not found")
        return {**dict(u), **self.stats(uid), "pro": self.is_pro(uid)}   # {**a, **b} merges dictionaries

    def list_listings(self):
        out = []
        for r in self._q("SELECT l.id, l.seller_id, l.title, l.price, (l.image IS NOT NULL) AS has_image, u.name seller_name FROM listings l JOIN users u ON u.id=l.seller_id ORDER BY l.id DESC"):
            d = dict(r); d["fee"] = self.local_fee(d["seller_id"], d["price"]); d["pro"] = self.is_pro(d["seller_id"])
            out.append(d)
        return out

    def add_trip(self, traveler_id, country, kg, date):
        if country not in TRAVELER_RATE_PER_KG: raise EscrowError("country not supported")
        if not 0 < kg <= 30: raise EscrowError("available space must be between 0 and 30 kg")
        c = self._q("INSERT INTO trips(traveler_id,country,kg_left,date) VALUES(?,?,?,?)", traveler_id, country, kg, date)
        self.db.commit(); return c.lastrowid

    def list_trips(self):
        return [dict(r) for r in self._q("SELECT t.*, u.name traveler_name FROM trips t JOIN users u ON u.id=t.traveler_id WHERE kg_left>0 ORDER BY date")]

    def get_image(self, listing_id):                     # returns (photo bytes, mime type) or None
        r = self._q("SELECT image, mime FROM listings WHERE id=?", listing_id).fetchone()
        return (r["image"], r["mime"]) if r and r["image"] else None

    def quote_items(self, country, items):
        """Price a shopping list: convert each item to TND, add the traveler's reward (by weight) and our fee.
        items = [{"name","url","price","currency","qty","weight_kg"}, ...]"""
        if country not in TRAVELER_RATE_PER_KG: raise EscrowError("country not supported")
        if not items: raise EscrowError("Add at least one item")
        if len(items) > 15: raise EscrowError("Maximum 15 items per request")
        rates = currency.get_rates()["rates"]
        lines, total_price, total_kg = [], 0, 0.0
        for it in items:
            name, url = str(it.get("name", "")).strip()[:80], str(it.get("url", "")).strip()[:500]
            cur, price, qty, kg = it.get("currency", "TND"), float(it.get("price", 0)), int(it.get("qty", 1)), float(it.get("weight_kg", 0))
            if not name or price <= 0: raise EscrowError("Each item needs a name and a price above 0")
            if not 0.05 <= kg <= 30: raise EscrowError("Each item needs a weight between 0.05 and 30 kg")
            if not 1 <= qty <= 20: raise EscrowError("Quantity must be between 1 and 20")
            if url and not url.lower().startswith(("http://", "https://")): raise EscrowError("Links must start with http:// or https://")
            if cur not in rates: raise EscrowError("Currency not supported")
            tnd = int(round(price * qty * rates[cur] * 1000))          # price in millimes
            lines.append({"name": name, "url": url, "currency": cur, "price": price, "qty": qty,
                          "weight_kg": kg, "rate": round(rates[cur], 5), "price_tnd": tnd})
            total_price += tnd
            total_kg += kg * qty
        reward = int(TRAVELER_RATE_PER_KG[country] * total_kg)
        fee = reward * TRAVELER_FEE_PCT // 100
        return {"items": lines, "item_price": total_price, "weight_kg": round(total_kg, 3),
                "reward": reward, "fee": fee, "total": total_price + reward + fee}

    def _trip(self, trip_id):
        t = self._q("SELECT * FROM trips WHERE id=?", trip_id).fetchone()
        if not t: raise EscrowError("trip not found")
        return t

    def quote_trip_items(self, trip_id, items):
        return self.quote_items(self._trip(trip_id)["country"], items)

    def create_trip_order(self, buyer_id, trip_id, items):
        """One order can hold many items (links) for the same traveler."""
        t = self._trip(trip_id)
        q = self.quote_items(t["country"], items)
        if q["weight_kg"] > t["kg_left"]: raise EscrowError(f"This traveler only has {t['kg_left']} kg left")
        n = len(q["items"])
        title = q["items"][0]["name"] + (f" +{n - 1}" if n > 1 else "")
        oid = self._create("TRAVELER", buyer_id, t["traveler_id"], title, q["item_price"], q["reward"], q["fee"],
                           items=json.dumps(q["items"]))
        self._q("UPDATE trips SET kg_left=kg_left-? WHERE id=?", q["weight_kg"], trip_id); self.db.commit()
        return oid

    def list_orders(self, uid, only_state=None):
        sql = ("SELECT o.*, b.name buyer_name, p.name provider_name, "
               "(SELECT l.image IS NOT NULL FROM listings l WHERE l.id=o.listing_id) AS has_image "
               "FROM orders o JOIN users b ON b.id=o.buyer_id JOIN users p ON p.id=o.provider_id WHERE ")
        rows = (self._q(sql + "o.state=? ORDER BY o.id DESC", only_state) if only_state
                else self._q(sql + "(o.buyer_id=? OR o.provider_id=?) ORDER BY o.id DESC", uid, uid))
        out = []
        for r in rows:
            d = dict(r); d["payout"] = d["total"] - d["fee"]
            # SECURITY: only the BUYER ever receives the delivery code
            d["code"] = self.code_for(d["id"]) if d["buyer_id"] == uid and d["state"] in ("FUNDED", "SHIPPED") else None
            d["items"] = json.loads(d["items"]) if d["items"] else None     # the shopping list of a traveler order
            d.pop("nonce"); out.append(d)
        return out
