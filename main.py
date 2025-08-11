
from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional, Dict
import math

from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Numeric, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# --- DB setup ---
engine = create_engine("sqlite:///expensecalc.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Models ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    currency = Column(String, default="AUD")
    total_amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String, default="active")  # draft|active|finalized
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

class Invite(Base):
    __tablename__ = "invites"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    email = Column(String, nullable=False)
    role = Column(String, default="member")  # admin|member
    token = Column(String, unique=True, index=True)
    token_expires_at = Column(DateTime)
    accepted_at = Column(DateTime, nullable=True)

class Participant(Base):
    __tablename__ = "participants"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    display_name = Column(String, default="")

class Pledge(Base):
    __tablename__ = "pledges"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    participant_id = Column(Integer, ForeignKey("participants.id"))
    type = Column(String)  # equal|volunteer_overpay|underpay_bid
    value_type = Column(String, nullable=True)  # percent|fixed or None for 'equal'
    value = Column(Numeric(12, 2), nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# --- Auth (demo magic-link) ---
TOKENS: Dict[str, dict] = {}

def get_or_create_user(db: Session, email: str) -> User:
    u = db.query(User).filter_by(email=email).first()
    if not u:
        u = User(email=email)
        db.add(u); db.commit(); db.refresh(u)
    return u

app = FastAPI(title="Expense Calculator Demo")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.middleware("http")
async def add_demo_user(request: Request, call_next):
    token = request.query_params.get("token")
    if token and token in TOKENS and TOKENS[token]["exp"] > datetime.utcnow():
        request.state.user_email = TOKENS[token]["email"]
    else:
        request.state.user_email = None
    response = await call_next(request)
    return response

def require_user(request: Request, db: Session) -> User:
    if not request.state.user_email:
        raise HTTPException(401, "Login required. Use an invite link with ?token=...")
    return get_or_create_user(db, request.state.user_email)

# --- Allocation helpers ---
def cents(d): return int(round(float(d) * 100))
def money(c): return round(c / 100.0 + 1e-9, 2)

def compute_allocations(db: Session, event_id: int):
    ev = db.query(Event).get(event_id)
    parts = db.query(Participant).filter_by(event_id=event_id).all()
    if not parts: return {}
    N = len(parts)
    total_cents = cents(ev.total_amount)
    base = total_cents // N
    targets = {p.id: base for p in parts}
    remainder = total_cents - base * N
    for p in parts[:remainder]: targets[p.id] += 1

    # Volunteer overpay
    vops = db.query(Pledge).filter_by(event_id=event_id, type="volunteer_overpay", active=True).all()
    for v in vops:
        add = 0
        if v.value_type == "percent":
            add = int(round(targets[v.participant_id] * float(v.value) / 100.0))
        elif v.value_type == "fixed":
            add = cents(v.value)
        targets[v.participant_id] += add

    # Underpay bids (auto-active in this demo)
    bids = db.query(Pledge).filter_by(event_id=event_id, type="underpay_bid", active=True).all()
    for b in bids:
        shortfall = 0
        if b.value_type == "percent":
            shortfall = int(round(targets[b.participant_id] * float(b.value) / 100.0))
        elif b.value_type == "fixed":
            shortfall = min(cents(b.value), targets[b.participant_id])
        if shortfall <= 0: 
            continue
        targets[b.participant_id] -= shortfall
        others = [pid for pid in targets if pid != b.participant_id]
        denom = sum(targets[pid] for pid in others) or 1
        add_map = {pid: int((shortfall * targets[pid]) // denom) for pid in others}
        distributed = sum(add_map.values())
        # fix leftover rounding
        for pid in sorted(others, key=lambda x: targets[x], reverse=True)[:shortfall - distributed]:
            add_map[pid] += 1
        for pid, inc in add_map.items():
            targets[pid] += inc

    # Final tiny correction
    gap = total_cents - sum(targets.values())
    if gap != 0:
        targets[max(targets, key=targets.get)] += gap

    return {pid: money(amt) for pid, amt in targets.items()}

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})

@app.post("/auth/request-magic-link")
def request_magic_link(email: EmailStr):
    token = str(uuid4())
    TOKENS[token] = {"email": email, "exp": datetime.utcnow() + timedelta(hours=2)}
    return {"login_url": f"/events?token={token}"}

@app.get("/events", response_class=HTMLResponse)
def list_events(request: Request, db: Session = Depends(get_db)):
    email = request.state.user_email
    events = db.query(Event).all()
    return templates.TemplateResponse("events.html", {"request": request, "email": email, "events": events})

@app.post("/events/create")
def create_event(request: Request, title: str = Form(...), description: str = Form(""),
                 currency: str = Form("AUD"), total_amount: float = Form(...),
                 db: Session = Depends(get_db)):
    user = require_user(request, db)
    ev = Event(title=title, description=description, currency=currency,
               total_amount=total_amount, created_by=user.id, status="active")
    db.add(ev); db.commit(); db.refresh(ev)
    p = Participant(event_id=ev.id, user_id=user.id, display_name=user.email.split("@")[0])
    db.add(p); db.commit()
    return RedirectResponse(url=f"/event/{ev.id}?token={request.query_params.get('token')}", status_code=303)

@app.get("/event/{event_id}", response_class=HTMLResponse)
def event_page(event_id: int, request: Request, db: Session = Depends(get_db)):
    email = request.state.user_email
    ev = db.query(Event).get(event_id)
    parts = db.query(Participant).filter_by(event_id=event_id).all()
    pledges = db.query(Pledge).filter_by(event_id=event_id).all()
    allocs = compute_allocations(db, event_id)
    return templates.TemplateResponse("event.html", {"request": request, "email": email, "ev": ev,
        "parts": parts, "pledges": pledges, "allocs": allocs})

@app.post("/event/{event_id}/invite")
def invite(event_id: int, request: Request, invite_email: EmailStr = Form(...), db: Session = Depends(get_db)):
    require_user(request, db)
    token = str(uuid4())
    inv = Invite(event_id=event_id, email=invite_email, role="member",
                 token=token, token_expires_at=datetime.utcnow()+timedelta(days=7))
    db.add(inv); db.commit()
    return RedirectResponse(url=f"/event/{event_id}?token={request.query_params.get('token')}", status_code=303)

@app.get("/event/{event_id}/join/{token}", response_class=HTMLResponse)
def join(event_id: int, token: str, request: Request, db: Session = Depends(get_db)):
    inv = db.query(Invite).filter_by(event_id=event_id, token=token).first()
    if not inv or inv.token_expires_at < datetime.utcnow():
        raise HTTPException(400, "Invalid or expired invite")
    inv.accepted_at = datetime.utcnow()
    user = get_or_create_user(db, inv.email)
    exists = db.query(Participant).filter_by(event_id=event_id, user_id=user.id).first()
    if not exists:
        db.add(Participant(event_id=event_id, user_id=user.id, display_name=user.email.split("@")[0]))
    db.commit()
    return RedirectResponse(url=f"/event/{event_id}?token={request.query_params.get('token')}", status_code=303)

@app.post("/event/{event_id}/pledge")
def pledge(event_id: int, request: Request, participant_id: int = Form(...),
           ptype: str = Form(...), value_type: Optional[str] = Form(None),
           value: Optional[float] = Form(None), db: Session = Depends(get_db)):
    require_user(request, db)
    active = ptype != "underpay_bid"  # bids would require approvals in production
    db.add(Pledge(event_id=event_id, participant_id=participant_id, type=ptype,
                  value_type=value_type, value=value, active=active))
    db.commit()
    return RedirectResponse(url=f"/event/{event_id}?token={request.query_params.get('token')}", status_code=303)

@app.get("/event/{event_id}/chart-data")
def chart_data(event_id: int, db: Session = Depends(get_db)):
    allocs = compute_allocations(db, event_id)
    parts = db.query(Participant).filter_by(event_id=event_id).all()
    labels = [p.display_name or f"User {p.user_id}" for p in parts]
    values = [allocs.get(p.id, 0.0) for p in parts]
    return {"labels": labels, "values": values}
