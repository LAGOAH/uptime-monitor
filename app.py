from flask import Flask, redirect, url_for, request, flash, get_flashed_messages, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
import os
import time
import requests
from datetime import datetime, timezone
import secrets
import resend
import logging
import fcntl
from concurrent.futures import ThreadPoolExecutor

# Configure production standard logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

app = Flask(__name__)

# Production Secret Key Setup
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-only-in-local")

# Cryptographic Token Serializer Definition for Password Resets
serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

# Database Setup & Configuration
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uptime.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# High-Concurrency Connection Pooling Options
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_size": 10,
    "max_overflow": 20,
    "pool_pre_ping": True  # Automatically reconnect dropouts
}

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# Third-Party Production API Hooks
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")
DEFAULT_FROM_EMAIL = "Uptime Monitor <onboarding@resend.dev>"

# ---------- MODELS ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_pro = db.Column(db.Boolean, default=False)
    
    # Password Reset Protocol Fields
    reset_token = db.Column(db.String(100), nullable=True, unique=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    
    websites = db.relationship("Website", backref="owner", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Website(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    alerts = db.relationship("Alert", backref="website", cascade="all, delete-orphan", lazy=True)
    history = db.relationship("ResponseHistory", backref="website", cascade="all, delete-orphan", lazy=True)

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    website_id = db.Column(db.Integer, db.ForeignKey("website.id"), nullable=False)
    last_status = db.Column(db.String(10), nullable=False)
    last_alert_sent = db.Column(db.DateTime, nullable=True)

class ResponseHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    website_id = db.Column(db.Integer, db.ForeignKey("website.id"), nullable=False)
    status = db.Column(db.String(10), nullable=False)
    response_time = db.Column(db.Float, nullable=True) 
    checked_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ---------- HELPER FUNCTIONS ----------
def check_website(url):
    try:
        start = datetime.now()
        r = requests.get(url, timeout=4, headers={"User-Agent": "UptimeTargetValidator/1.0"})
        rt = round((datetime.now() - start).total_seconds(), 3)
        
        if "There isn't a GitHub Pages site here" in r.text or "404 Not Found" in r.text or "Site not found" in r.text:
            return "DOWN", rt

        if 200 <= r.status_code < 400:
            return "UP", rt
        else:
            return "DOWN", rt
    except Exception:
        return "DOWN", None

def send_alert_email(user_email, website_name, status, url, response_time="N/A"):
    if not resend.api_key:
        app.logger.warning("Resend API key missing – email aborted")
        return False
        
    time.sleep(0.3)
    subject = f"⚠️ Alert: {website_name} is {status}"
    html = f"""
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">📡 Uptime Monitor Notification</h2>
        <p>Your website <strong>{website_name}</strong> (<a href="{url}" style="color:#818cf8;">{url}</a>) status shifted to <strong style="color:{'#10b981' if status=='UP' else '#ef4444'};">{status}</strong>.</p>
        <p><strong>Latency Telemetry:</strong> {response_time} seconds</p>
    </div>
    """
    try:
        resend.Emails.send({
            "from": DEFAULT_FROM_EMAIL,
            "to": [user_email],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        app.logger.error(f"Email failure: {e}")
        return False

def send_welcome_email(user_email):
    if not resend.api_key:
        return False
    subject = "Welcome to Pulse Uptime Monitor! 🚀"
    html = """
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">Welcome aboard! 🚀</h2>
        <p>You have successfully initialized your operational control unit account on Pulse.</p>
    </div>
    """
    try:
        resend.Emails.send({"from": DEFAULT_FROM_EMAIL, "to": [user_email], "subject": subject, "html": html})
        return True
    except Exception as e:
        app.logger.error(f"Welcome failure: {e}")
        return False

def send_reset_email(user_email, reset_url):
    if not resend.api_key:
        return False
    subject = "Secure Password Reset Protocol Request"
    html = f"""
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">Password Authorization Reset</h2>
        <a href="{reset_url}" style="background-color:#4f46e5; color:#ffffff; padding:12px 24px; border-radius:8px; text-decoration:none;">Reset Credentials</a>
    </div>
    """
    try:
        resend.Emails.send({"from": DEFAULT_FROM_EMAIL, "to": [user_email], "subject": subject, "html": html})
        return True
    except Exception:
        return False

def process_single_website(website_id):
    # CRITICAL FIX: Run explicitly inside context using standalone ID to prevent thread session dropouts
    with app.app_context():
        try:
            website = db.session.get(Website, website_id)
            if not website:
                return
            user = db.session.get(User, website.user_id)
            if not user:
                return
            
            current_status, response_time = check_website(website.url)
            
            history_record = ResponseHistory(
                website_id=website.id,
                status=current_status,
                response_time=response_time
            )
            db.session.add(history_record)
            
            alert = Alert.query.filter_by(website_id=website.id).first()
            if not alert:
                alert = Alert(website_id=website.id, last_status=current_status)
                db.session.add(alert)
                db.session.commit()
                return

            if alert.last_status != current_status:
                send_alert_email(user.email, website.name, current_status, website.url, response_time if response_time else "N/A")
                alert.last_status = current_status
                alert.last_alert_sent = datetime.now()
                
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Scan exception: {e}")

# ---------- COMPLIANT FAVICON PATH DELIVERY ----------
@app.route('/favicon.ico')
@app.route('/static/favicon.svg')
def favicon():
    # Flask 3.1.x safe absolute safe location resolution
    return send_from_directory(
        os.path.join(app.root_path, 'static'), 
        'favicon.svg', 
        mimetype='image/svg+xml'
    )

# ---------- ROUTES ----------

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return '''
<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📡 Pulse | Autonomous Uptime Infrastructure</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #030712; }
    </style>
</head>
<body class="text-gray-100 overflow-x-hidden selection:bg-indigo-500/30">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-7xl h-[600px] pointer-events-none opacity-30 blur-[140px] bg-gradient-to-r from-indigo-600 via-purple-600 to-pink-600 rounded-full top-[-250px]"></div>

    <nav class="sticky top-0 z-50 backdrop-blur-md bg-gray-950/70 border-b border-gray-800/60 transition-all">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-2.5">
                <div class="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center shadow-[0_0_20px_rgba(99,102,241,0.5)]">
                    <i class="fas fa-satellite-dish text-white text-sm animate-pulse"></i>
                </div>
                <span class="text-lg font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white via-gray-200 to-gray-400">PULSE</span>
            </div>
            <div class="flex items-center gap-3 sm:gap-4">
                <a href="mailto:lazarusgodswillahmadu@gmail.com?subject=Pulse%20Support%20Request" class="text-sm font-medium text-gray-400 hover:text-white transition px-3 py-1.5 rounded-lg hover:bg-gray-900 flex items-center gap-1.5">
                    <i class="fas fa-headset text-indigo-400"></i> Contact Us
                </a>
                <a href="/login" class="text-sm font-medium text-gray-400 hover:text-white transition px-3 py-1.5 rounded-lg hover:bg-gray-900">Log in</a>
                <a href="/signup" class="text-sm font-medium bg-white text-gray-950 px-4 py-2 rounded-xl hover:bg-gray-200 transition shadow-[0_4px_20px_rgba(255,255,255,0.15)] transform active:scale-95">Sign up free</a>
            </div>
        </div>
    </nav>

    <section class="relative max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 pt-24 pb-16 text-center z-10">
        <div class="inline-flex items-center gap-2 bg-indigo-500/10 border border-indigo-500/30 rounded-full px-3.5 py-1.5 mb-6">
            <span class="w-2 h-2 rounded-full bg-indigo-400 animate-ping"></span>
            <span class="text-xs font-semibold tracking-wide text-indigo-300 uppercase">Engine V2.4 Live</span>
        </div>
        <h1 class="text-4xl sm:text-6xl lg:text-7xl font-extrabold tracking-tight text-white leading-[1.1] mb-6">
            Intelligent infrastructure <br class="hidden sm:inline">
            <span class="bg-clip-text text-transparent bg-gradient-to-r from-indigo-400 via-purple-400 to-pink-400">monitored in real-time.</span>
        </h1>
        <p class="text-base sm:text-xl text-gray-400 max-w-2xl mx-auto mb-10 leading-relaxed">
            Eliminate blindspots. Pulse orchestrates zero-overhead, multi-node availability validation hooks with instant, high-priority email escalation arrays.
        </p>
        <div class="flex flex-col sm:flex-row justify-center items-center gap-4">
            <a href="/signup" class="w-full sm:w-auto bg-indigo-600 hover:bg-indigo-500 text-white px-8 py-4 rounded-xl font-semibold shadow-[0_0_30px_rgba(99,102,241,0.4)] transition transform hover:-translate-y-0.5">
                Launch Dashboard — Free
            </a>
        </div>
    </section>

    <footer class="border-t border-gray-800/80 bg-gray-950/40 py-8 relative z-10">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row justify-between items-center gap-4 text-center sm:text-left">
            <span class="text-xs text-gray-500">© 2026 Pulse Systems Inc. All rights reserved. Engineered for performance.</span>
        </div>
    </footer>
</body>
</html>
    '''

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if User.query.filter_by(email=email).first():
            flash("Email already registered")
            return redirect(url_for("signup"))
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        send_welcome_email(email)
        login_user(user)
        return redirect(url_for("dashboard"))
    return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pulse | Provision Node Cluster</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="bg-gray-900/50 border border-gray-800/80 p-8 rounded-2xl w-full max-w-md backdrop-blur-md">
        <h2 class="text-2xl font-bold text-center text-white mb-6">Create your account</h2>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1">Email Address</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 text-white" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1">Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 text-white" required></div>
            <button type="submit" class="w-full bg-indigo-600 text-white py-3 rounded-xl text-sm font-semibold">Provision Account</button>
        </form>
    </div>
</body>
</html>
    '''

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Invalid email or password")
        
    flash_messages = "".join([f'<div class="text-red-400 text-xs text-center mb-4">⚠️ {m}</div>' for m in get_flashed_messages()])

    return f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pulse | Secure Access Portal</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="bg-gray-900/50 border border-gray-800/80 p-8 rounded-2xl w-full max-w-md">
        <h2 class="text-2xl font-bold text-center text-white mb-6">Access Portal</h2>
        {flash_messages}
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1">Email</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-white focus:border-indigo-500" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1">Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-white focus:border-indigo-500" required></div>
            <button type="submit" class="w-full bg-indigo-600 text-white py-3 rounded-xl text-sm font-semibold">Log In</button>
        </form>
    </div>
</body>
</html>
    '''

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

@app.route("/api/status")
@login_required
def api_status():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    data = []
    for w in websites:
        status, rt = check_website(w.url)
        data.append({"id": w.id, "name": w.name, "url": w.url, "status": status, "response_time": rt if rt else "N/A"})
    return {"websites": data}

@app.route("/dashboard")
@login_required
def dashboard():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    cards = ""
    for w in websites:
        status, rt = check_website(w.url)
        status_color = "emerald" if status == "UP" else "red"
        status_bg = "emerald-500/10" if status == "UP" else "red-500/10"
        status_border = "emerald-500/30" if status == "UP" else "red-500/30"
        
        cards += f'''
        <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-5 backdrop-blur-sm flex flex-col justify-between">
            <div class="flex justify-between items-start gap-3 w-full">
                <div class="min-w-0 flex-1">
                    <h3 class="font-bold text-white text-base truncate">{w.name}</h3>
                    <p class="text-gray-400 text-xs truncate mt-0.5">{w.url}</p>
                </div>
                <div class="flex flex-col items-end shrink-0">
                    <span class="bg-{status_bg} border border-{status_border} px-2.5 py-1 rounded-full text-xs font-semibold text-{status_color}-400">{status}</span>
                    <span class="text-gray-500 text-[10px] mt-1">{rt if rt else 'N/A'} SEC</span>
                </div>
            </div>
            <div class="mt-6 pt-3 border-t border-gray-800/60 flex justify-between items-center w-full">
                <span class="text-[10px] font-mono text-gray-500">NODE #{w.id}</span>
                <a href="/delete-website/{w.id}" class="text-red-400 hover:text-red-300 text-xs flex items-center gap-1"><i class="fas fa-trash-can text-[10px]"></i> Terminate</a>
            </div>
        </div>
        '''
    
    flash_messages = "".join([f'<div class="bg-indigo-950/60 text-indigo-300 text-xs px-4 py-3 rounded-xl mb-6">⚠️ {m}</div>' for m in get_flashed_messages()])
    pro_badge = '⭐ PRO ACTIVE' if current_user.is_pro else '<a href="/upgrade" class="bg-indigo-600 text-white px-3 py-1 rounded-full text-xs font-bold">Upgrade to Pro</a>'
    
    return f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pulse Core | Dashboard</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-[#030712] text-gray-100 min-h-screen">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10">
        <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-gray-800 pb-6 mb-8">
            <div>
                <span class="text-[10px] font-mono text-indigo-400">OPERATIONAL CONTROL UNIT</span>
                <h1 class="text-xl sm:text-2xl font-bold text-white truncate">{current_user.email}</h1>
                <div class="mt-1">{pro_badge}</div>
            </div>
            <div class="flex items-center gap-2.5 w-full sm:w-auto">
                <a href="mailto:lazarusgodswillahmadu@gmail.com?subject=Pulse%20Support" class="border border-gray-800 bg-gray-900/50 text-gray-300 px-4 py-2 rounded-xl text-sm font-semibold flex items-center gap-1.5">
                    <i class="fas fa-headset text-indigo-400"></i> Contact Support
                </a>
                <a href="/add-website" class="bg-white text-gray-950 px-4 py-2 rounded-xl text-sm font-semibold">+ Inject Node</a>
                <a href="/logout" class="bg-gray-900 border border-gray-800 text-gray-400 px-4 py-2 rounded-xl text-sm"><i class="fas fa-right-from-bracket"></i></a>
            </div>
        </div>
        <div id="flash-container">{flash_messages}</div>
        <div id="cards-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">{cards if cards else '<div class="col-span-full text-center py-12 text-gray-500">No active tracking infrastructure.</div>'}</div>
    </div>
</body>
</html>
    '''

@app.route("/add-website", methods=["GET", "POST"])
@login_required
def add_website():
    if request.method == "POST":
        name = request.form["name"].strip()
        url = request.form["url"].strip().rstrip('/')
        
        if not current_user.is_pro and Website.query.filter_by(user_id=current_user.id).count() >= 3:
            flash("Free tier limit reached! Upgrade to Pro for unlimited URLs.")
            return redirect(url_for("dashboard"))
            
        website = Website(name=name, url=url, user_id=current_user.id)
        db.session.add(website)
        db.session.commit()
        return redirect(url_for("dashboard"))
        
    return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Pulse | Mount Target</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="bg-gray-900/50 border border-gray-800 p-8 rounded-2xl w-full max-w-md">
        <h2 class="text-xl font-bold text-white mb-4">Provision Monitoring Node</h2>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 block mb-1">Title</label><input name="name" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm text-white" required></div>
            <div><label class="text-xs text-gray-400 block mb-1">Target URL</label><input name="url" type="url" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm text-white" required></div>
            <div class="flex gap-3"><a href="/dashboard" class="w-1/3 text-center border border-gray-800 py-3 rounded-xl text-sm">Cancel</a><button type="submit" class="w-2/3 bg-white text-gray-950 py-3 rounded-xl text-sm font-semibold">Mount Node</button></div>
        </form>
    </div>
</body>
</html>
    '''

@app.route("/delete-website/<int:website_id>")
@login_required
def delete_website(website_id):
    website = db.session.get(Website, website_id)
    if website and website.user_id == current_user.id:
        db.session.delete(website)
        db.session.commit()
    return redirect(url_for("dashboard"))

@app.route("/upgrade")
@login_required
def upgrade():
    if not PAYSTACK_SECRET_KEY:
        return "Paystack Key Missing", 500
    ref = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    data = {"amount": 1500000, "email": current_user.email, "reference": ref, "callback_url": url_for("payment_success", _external=True)}
    try:
        r = requests.post("https://api.paystack.co/transaction/initialize", json=data, headers=headers)
        result = r.json()
        if result.get("status"):
            return redirect(result["data"]["authorization_url"])
        return redirect(url_for("dashboard"))
    except Exception:
        return redirect(url_for("dashboard"))

@app.route("/payment-success")
def payment_success():
    ref = request.args.get("reference")
    if ref:
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        try:
            r = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers)
            result = r.json()
            if result.get("status") and result["data"]["status"] == "success":
                email = result["data"]["customer"]["email"]
                user = User.query.filter_by(email=email).first()
                if user:
                    user.is_pro = True
                    db.session.commit()
        except Exception:
            pass
    return redirect(url_for("dashboard"))

@app.route("/update-all")
def update_all():
    lock_file = open("/tmp/update_automation.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return "Running", 429
    try:
        # Pass integer IDs directly to avoid out-of-scope session termination issues inside the background pool threads
        website_ids = [w.id for w in Website.query.all()]
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(process_single_website, website_ids)
        return "OK", 200
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
