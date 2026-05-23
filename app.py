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

# Cryptographic Token Serializer for Password Resets
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
    "pool_pre_ping": True
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

# Email Rate-Limit Aware Retry Loop Block
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
        <p style="color:#9ca3af; font-size:12px;">Event log: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    """
    for attempt in range(3):
        try:
            resend.Emails.send({
                "from": DEFAULT_FROM_EMAIL,
                "to": [user_email],
                "subject": subject,
                "html": html,
            })
            app.logger.info(f"Alert sent to {user_email}")
            return True
        except Exception as e:
            app.logger.error(f"Email attempt {attempt+1} failed: {e}")
            time.sleep(1.5 if "429" in str(e) else 0.5)
    return False

def send_welcome_email(user_email):
    if not resend.api_key:
        return False
    subject = "Welcome to Pulse Uptime Monitor! 🚀"
    html = f"""
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">Welcome aboard! 🚀</h2>
        <p>You have successfully initialized your operational control unit account on Pulse.</p>
        <p>Start mounting infrastructure targets immediately to capture real-time availability logs.</p>
    </div>
    """
    try:
        resend.Emails.send({"from": DEFAULT_FROM_EMAIL, "to": [user_email], "subject": subject, "html": html})
        return True
    except Exception as e:
        app.logger.error(f"Welcome email failed: {e}")
        return False

def send_reset_email(user_email, reset_url):
    if not resend.api_key:
        return False
    subject = "Secure Password Reset Protocol Request"
    html = f"""
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">Password Authorization Reset</h2>
        <p>An access credential modification request was submitted. Use the token link below to define new access values. This security link expires in 1 hour.</p>
        <div style="margin: 24px 0;">
            <a href="{reset_url}" style="background-color:#4f46e5; color:#ffffff; padding:12px 24px; border-radius:8px; text-decoration:none; font-weight:bold;">Reset Access Credentials</a>
        </div>
        <p style="color:#9ca3af; font-size:11px;">If you did not request this, ignore this automated transmission.</p>
    </div>
    """
    try:
        resend.Emails.send({"from": DEFAULT_FROM_EMAIL, "to": [user_email], "subject": subject, "html": html})
        return True
    except Exception as e:
        app.logger.error(f"Reset email failed: {e}")
        return False

def process_single_website(website_id):
    with app.app_context():
        try:
            website = db.session.get(Website, website_id)
            if not website:
                return
            user = db.session.get(User, website.user_id)
            if not user:
                return
            current_status, response_time = check_website(website.url)
            history_record = ResponseHistory(website_id=website.id, status=current_status, response_time=response_time)
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
            app.logger.error(f"Process error: {e}")

# ---------- FAVICON ROUTE (SVG) ----------
@app.route('/favicon.ico')
@app.route('/static/favicon.svg')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.svg', mimetype='image/svg+xml')

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
    <style>@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');body{font-family:'Plus Jakarta Sans',sans-serif;background-color:#030712;}</style>
</head>
<body class="text-gray-100 overflow-x-hidden selection:bg-indigo-500/30">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-7xl h-[600px] pointer-events-none opacity-30 blur-[140px] bg-gradient-to-r from-indigo-600 via-purple-600 to-pink-600 rounded-full top-[-250px]"></div>
    <nav class="sticky top-0 z-50 backdrop-blur-md bg-gray-950/70 border-b border-gray-800/60 transition-all">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-2.5">
                <div class="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center shadow-[0_0_20px_rgba(99,102,241,0.5)]"><i class="fas fa-satellite-dish text-white text-sm animate-pulse"></i></div>
                <span class="text-lg font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white via-gray-200 to-gray-400">PULSE</span>
            </div>
            <div class="flex items-center gap-3 sm:gap-4">
                <a href="/login" class="text-sm font-medium text-gray-400 hover:text-white transition px-3 py-1.5 rounded-lg hover:bg-gray-900">Log in</a>
                <a href="/signup" class="text-sm font-medium bg-white text-gray-950 px-4 py-2 rounded-xl hover:bg-gray-200 transition shadow-[0_4px_20px_rgba(255,255,255,0.15)] transform active:scale-95">Sign up free</a>
            </div>
        </div>
    </nav>
    <section class="relative max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 pt-24 pb-16 text-center z-10">
        <div class="inline-flex items-center gap-2 bg-indigo-500/10 border border-indigo-500/30 rounded-full px-3.5 py-1.5 mb-6 transform hover:scale-105 transition-all cursor-pointer"><span class="w-2 h-2 rounded-full bg-indigo-400 animate-ping"></span><span class="text-xs font-semibold tracking-wide text-indigo-300 uppercase">Engine V2.4 Live</span></div>
        <h1 class="text-4xl sm:text-6xl lg:text-7xl font-extrabold tracking-tight text-white leading-[1.1] mb-6">Intelligent infrastructure <br class="hidden sm:inline"><span class="bg-clip-text text-transparent bg-gradient-to-r from-indigo-400 via-purple-400 to-pink-400">monitored in real-time.</span></h1>
        <p class="text-base sm:text-xl text-gray-400 max-w-2xl mx-auto mb-10 leading-relaxed">Eliminate blindspots. Pulse orchestrates zero-overhead, multi-node availability validation hooks with instant, high-priority email escalation arrays.</p>
        <div class="flex flex-col sm:flex-row justify-center items-center gap-4">
            <a href="/signup" class="w-full sm:w-auto bg-indigo-600 hover:bg-indigo-500 text-white px-8 py-4 rounded-xl font-semibold shadow-[0_0_30px_rgba(99,102,241,0.4)] transition transform hover:-translate-y-0.5">Launch Dashboard — Free</a>
            <a href="#pricing" class="w-full sm:w-auto border border-gray-800 bg-gray-900/40 hover:bg-gray-900 text-gray-300 px-8 py-4 rounded-xl font-semibold transition">Explore Analytics</a>
        </div>
    </section>
    <section class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition"><div class="w-10 h-10 rounded-lg bg-indigo-500/10 flex items-center justify-center text-indigo-400 mb-4"><i class="fas fa-bolt"></i></div><h3 class="text-lg font-bold text-white mb-2">High-Velocity Scans</h3><p class="text-sm text-gray-400 leading-relaxed">Parallel engine blocks cycle every 60 seconds natively to isolate outages instantaneously.</p></div>
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition"><div class="w-10 h-10 rounded-lg bg-purple-500/10 flex items-center justify-center text-purple-400 mb-4"><i class="fas fa-bell"></i></div><h3 class="text-lg font-bold text-white mb-2">Instant Escalations</h3><p class="text-sm text-gray-400 leading-relaxed">Direct routing loops into Resend API transactional channels to bypass standard inbox delay traps.</p></div>
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition"><div class="w-10 h-10 rounded-lg bg-pink-500/10 flex items-center justify-center text-pink-400 mb-4"><i class="fas fa-chart-bar"></i></div><h3 class="text-lg font-bold text-white mb-2">Granular Latency Logs</h3><p class="text-sm text-gray-400 leading-relaxed">Capture precise telemetry tracking metrics across execution runs to preserve analytical records.</p></div>
        </div>
    </section>
    <section id="pricing" class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 relative z-10">
        <div class="text-center mb-12"><h2 class="text-3xl sm:text-5xl font-extrabold text-white mb-3">Predictable pricing paradigms</h2><p class="text-gray-400 text-sm sm:text-base">Scale your visibility bounds without micro-transaction penalties.</p></div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-4xl mx-auto items-stretch">
            <div class="bg-gray-900/30 border border-gray-800/80 rounded-3xl p-8 flex flex-col justify-between backdrop-blur-md shadow-2xl relative overflow-hidden group"><div><h3 class="text-xl font-bold text-white mb-1">Standard Node</h3><p class="text-xs text-gray-500 mb-6">Perfect for static personal landing deployments.</p><div class="flex items-baseline gap-1.5 mb-6"><span class="text-4xl font-extrabold text-white">₦0</span><span class="text-xs text-gray-500">/ forever</span></div><ul class="space-y-4 border-t border-gray-800/80 pt-6"><li class="flex items-center gap-3 text-sm text-gray-300"><i class="fas fa-circle-check text-indigo-500 text-xs"></i> 3 Monitored Endpoints</li><li class="flex items-center gap-3 text-sm text-gray-300"><i class="fas fa-circle-check text-indigo-500 text-xs"></i> Standard Email Triggers</li><li class="flex items-center gap-3 text-sm text-gray-400 opacity-50"><i class="fas fa-circle-xmark text-gray-600 text-xs"></i> 1-Minute Premium Checks</li></ul></div><a href="/signup" class="block w-full text-center bg-gray-800 hover:bg-gray-700 text-white font-medium text-sm py-3 rounded-xl mt-8 transition">Deploy Free Cluster</a></div>
            <div class="bg-gradient-to-b from-indigo-950/40 to-gray-950/40 border-2 border-indigo-500/80 rounded-3xl p-8 flex flex-col justify-between backdrop-blur-md shadow-2xl relative overflow-hidden group shadow-[0_0_50px_-12px_rgba(99,102,241,0.3)]"><div class="absolute top-0 right-0 bg-indigo-500 text-white font-bold tracking-wider text-[10px] uppercase px-4 py-1 rounded-bl-xl shadow-md">Premium Tier</div><div><h3 class="text-xl font-bold text-white mb-1">Enterprise Pro</h3><p class="text-xs text-indigo-300 mb-6">For real-time operational scale applications.</p><div class="flex items-baseline gap-1.5 mb-6"><span class="text-4xl font-extrabold text-white">₦15,000</span><span class="text-xs text-gray-400">/ month</span></div><ul class="space-y-4 border-t border-indigo-900/50 pt-6"><li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> Unlimited Registered Endpoints</li><li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> High-Speed 1-Minute Diagnostics</li><li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> Detailed Latency History Logging</li></ul></div><a href="/signup" class="block w-full text-center bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-sm py-3 rounded-xl mt-8 shadow-lg shadow-indigo-600/30 transition">Provision Pro Engine</a></div>
        </div>
    </section>
    <footer class="border-t border-gray-800/80 bg-gray-950/40 py-8 relative z-10"><div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row justify-between items-center gap-4 text-center sm:text-left"><span class="text-xs text-gray-500">© 2026 Pulse Systems Inc. All rights reserved.</span><div class="flex items-center gap-4 text-xs"><a href="mailto:lazarusgodswillahmadu@gmail.com?subject=Pulse%20Support%20Request&body=Describe%20your%20issue..." class="text-gray-400 hover:text-white transition flex items-center gap-1.5"><i class="fas fa-headset text-indigo-400"></i> Contact Support</a><a href="#" class="text-gray-500 hover:text-white"><i class="fab fa-github"></i></a><a href="#" class="text-gray-500 hover:text-white"><i class="fab fa-twitter"></i></a></div></div></footer>
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pulse | Provision Node Cluster</title><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6"><h2 class="text-2xl font-bold text-white tracking-tight">Create your account</h2><p class="text-xs text-gray-400 mt-1">Instant telemetry routing initialization.</p></div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Email Address</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="you@domain.com" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Secret Token Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="••••••••" required></div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-xl text-sm font-semibold shadow-lg shadow-indigo-600/20 transition active:scale-[0.99]">Provision Account</button>
        </form>
        <p class="text-xs text-center text-gray-500 mt-4">Existing credentials? <a href="/login" class="text-indigo-400 hover:underline">Log in instead</a></p>
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
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div class="bg-red-950/50 border border-red-500/30 text-red-300 text-xs px-4 py-3 rounded-xl mb-4 text-center">⚠️ {msg}</div>'
    return f'''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pulse | Secure Access Control Interface</title><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6"><h2 class="text-2xl font-bold text-white tracking-tight">Access Control Interface</h2><p class="text-xs text-gray-400 mt-1">Authenticate access protocols.</p></div>
        {flash_messages}
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Email Address</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="you@domain.com" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="••••••••" required></div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-xl text-sm font-semibold shadow-lg shadow-indigo-600/20 transition active:scale-[0.99]">Establish Authentication</button>
        </form>
        <div class="flex flex-col gap-2 text-center mt-4 text-xs text-gray-500">
            <p>Forgotten access token? <a href="/forgot-password" class="text-indigo-400 hover:underline">Reset access keys</a></p>
            <p>Missing authorization? <a href="/signup" class="text-indigo-400 hover:underline">Register cluster node</a></p>
        </div>
    </div>
</body>
</html>
    '''

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = serializer.dumps(email, salt="password-reset-salt")
            reset_url = url_for("reset_password", token=token, _external=True)
            send_reset_email(email, reset_url)
        flash("If that configuration coordinate exists inside our registry index, a recovery transmission has been dispatched.")
        return redirect(url_for("login"))
    return '''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pulse | Authorization Recovery Protocol</title><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6"><h2 class="text-xl font-bold text-white tracking-tight">Initialize Recovery Flow</h2><p class="text-xs text-gray-400 mt-1">Submit registered connection address token.</p></div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Account Email Address</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="you@domain.com" required></div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-xl text-sm font-semibold shadow-lg shadow-indigo-600/20 transition active:scale-[0.99]">Dispatch Recovery Link</button>
        </form>
        <p class="text-xs text-center text-gray-500 mt-4"><a href="/login" class="text-indigo-400 hover:underline">Return to interface portal</a></p>
    </div>
</body>
</html>
    '''

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="password-reset-salt", max_age=3600)
    except Exception:
        flash("The access credentials recovery security token is invalid or expired.")
        return redirect(url_for("forgot_password"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Administrative match record missing.")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form["password"]
        user.set_password(password)
        db.session.commit()
        flash("Account security layers updated. Enter new credentials to access dashboard.")
        return redirect(url_for("login"))
    return '''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pulse | Overwrite Security Credentials</title><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6"><h2 class="text-xl font-bold text-white tracking-tight">Overwrite Access Values</h2><p class="text-xs text-gray-400 mt-1">Establish highly secure fresh password block.</p></div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">New Security Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="••••••••" required></div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-xl text-sm font-semibold shadow-lg shadow-indigo-600/20 transition active:scale-[0.99]">Update Credentials Profile</button>
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

# ---------- API & DASHBOARD ----------
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
        pulse_animation = "animate-pulse" if status == "UP" else ""
        cards += f'''
        <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-5 backdrop-blur-sm shadow-xl flex flex-col justify-between group hover:border-gray-700 transition-all overflow-hidden max-w-full">
            <div class="flex justify-between items-start gap-3 w-full">
                <div class="min-w-0 flex-1">
                    <h3 class="font-bold text-white text-base truncate">{w.name}</h3>
                    <p class="text-gray-400 text-xs truncate mt-0.5" title="{w.url}">{w.url}</p>
                </div>
                <div class="flex flex-col items-end shrink-0">
                    <span class="inline-flex items-center gap-1.5 bg-{status_bg} border border-{status_border} px-2.5 py-1 rounded-full text-xs font-semibold text-{status_color}-400">
                        <span class="w-1.5 h-1.5 rounded-full bg-{status_color}-400 {pulse_animation}"></span>
                        {status}
                    </span>
                    <span class="text-gray-500 text-[10px] tracking-wide uppercase mt-1">{rt if rt else 'N/A'} SEC LATENCY</span>
                </div>
            </div>
            <div class="mt-6 pt-3 border-t border-gray-800/60 flex justify-between items-center w-full">
                <span class="text-[10px] font-mono text-gray-500 tracking-wider">NODE ID: #{w.id}</span>
                <a href="/delete-website/{w.id}" class="text-red-400 hover:text-red-300 text-xs font-medium inline-flex items-center gap-1 transition"><i class="fas fa-trash-can text-[10px]"></i> Terminate Node</a>
            </div>
        </div>'''
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div class="bg-indigo-950/60 border border-indigo-500/30 text-indigo-300 text-xs px-4 py-3 rounded-xl mb-6 shadow-lg flex items-center gap-2"><i class="fas fa-circle-info"></i> SYSTEM: {msg}</div>'
    pro_badge = '<span class="bg-gradient-to-r from-amber-400 to-yellow-500 text-gray-950 px-3 py-1 rounded-full text-xs font-extrabold tracking-tight shadow-md shadow-yellow-500/10">⭐ PRO ACTIVE</span>' if current_user.is_pro else '<a href="/upgrade" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-full text-xs font-bold shadow-md shadow-indigo-600/20 transition active:scale-95">Upgrade to Pro</a>'
    support_mailto = f"mailto:lazarusgodswillahmadu@gmail.com?subject=Pulse%20Core%20Support%20Request&body=Describe%20your%20issue...%0A%0A---%0AUser%20Email%3A%20{current_user.email}%0AApp%20Version%3A%20v2.4"
    template = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pulse Core | Operational Control Unit</title>
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');body{{font-family:'Plus Jakarta Sans',sans-serif;background-color:#030712;}}</style>
</head>
<body class="text-gray-100 selection:bg-indigo-500/30 overflow-x-hidden min-h-screen flex flex-col justify-between">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-7xl h-[400px] pointer-events-none opacity-20 blur-[120px] bg-indigo-600 rounded-full top-[-200px]"></div>
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-10 relative z-10">
        <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-gray-800/80 pb-6 mb-8">
            <div class="min-w-0">
                <span class="text-[10px] font-mono tracking-widest uppercase text-indigo-400">OPERATIONAL CONTROL UNIT</span>
                <h1 class="text-xl sm:text-3xl font-extrabold text-white tracking-tight truncate mt-0.5">Instance: __USER_EMAIL__</h1>
                <div class="mt-2">__PRO_BADGE__</div>
            </div>
            <div class="flex items-center gap-2.5 w-full sm:w-auto shrink-0">
                <a href="{support_mailto}" class="border border-gray-800 bg-gray-900/50 hover:bg-gray-800 text-gray-300 px-4 py-2.5 rounded-xl text-sm font-semibold transition flex items-center gap-1.5 shadow-sm"><i class="fas fa-headset text-indigo-400"></i> Contact Support</a>
                <a href="/add-website" class="flex-1 sm:flex-initial text-center bg-white hover:bg-gray-200 text-gray-950 px-4 py-2.5 rounded-xl text-sm font-semibold transition shadow-md shadow-white/5 active:scale-[0.98]">+ Inject Node</a>
                <button onclick="fetch('/update-all').then(() => refreshStatus())" class="flex-1 sm:flex-initial text-center bg-indigo-600 hover:bg-indigo-500 text-white px-4 py-2.5 rounded-xl text-sm font-semibold transition shadow-md shadow-indigo-600/20 active:scale-[0.98]"><i class="fas fa-rotate-right mr-1"></i> Force Check</button>
                <a href="/logout" class="bg-gray-900 border border-gray-800 hover:bg-gray-800 text-gray-400 px-4 py-2.5 rounded-xl text-sm font-semibold transition active:scale-[0.98]"><i class="fas fa-right-from-bracket"></i></a>
            </div>
        </div>
        <div id="flash-container">__FLASH_MESSAGES__</div>
        <div id="cards-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">__CARDS__</div>
    </div>
    <footer class="border-t border-gray-800/60 bg-gray-950/20 py-6 mt-12 relative z-10">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row justify-between items-center gap-4 text-center sm:text-left">
            <span class="text-xs text-gray-500">© 2026 Pulse Infrastructure Dashboard Node. Real-time telemetry connection stable.</span>
        </div>
    </footer>
    <script>
        async function refreshStatus() {{
            try {{
                const response = await fetch('/api/status');
                const data = await response.json();
                const websites = data.websites;
                const container = document.getElementById('cards-container');
                if (!container) return;
                if (websites.length === 0) {{
                    container.innerHTML = '<div class="col-span-full bg-gray-900/20 border border-dashed border-gray-800 rounded-2xl py-12 text-center text-sm text-gray-500 flex flex-col items-center justify-center gap-2"><i class="fas fa-folder-open text-xl opacity-40"></i> No infrastructure nodes configured to this cluster environment index.</div>';
                    return;
                }}
                let newCards = '';
                for (let w of websites) {{
                    const status = w.status;
                    const statusColor = status === 'UP' ? 'emerald' : 'red';
                    const statusBg = status === 'UP' ? 'emerald-500/10' : 'red-500/10';
                    const statusBorder = status === 'UP' ? 'emerald-500/30' : 'red-500/30';
                    const pulseAnimation = status === 'UP' ? 'animate-pulse' : '';
                    const responseTime = w.response_time !== 'N/A' ? w.response_time + ' SEC LATENCY' : 'N/A LATENCY';
                    newCards += `
                        <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-5 backdrop-blur-sm shadow-xl flex flex-col justify-between group hover:border-gray-700 transition-all overflow-hidden max-w-full">
                            <div class="flex justify-between items-start gap-3 w-full">
                                <div class="min-w-0 flex-1">
                                    <h3 class="font-bold text-white text-base truncate">${{w.name}}</h3>
                                    <p class="text-gray-400 text-xs truncate mt-0.5" title="${{w.url}}">${{w.url}}</p>
                                </div>
                                <div class="flex flex-col items-end shrink-0">
                                    <span class="inline-flex items-center gap-1.5 bg-${{statusBg}} border border-${{statusBorder}} px-2.5 py-1 rounded-full text-xs font-semibold text-${{statusColor}}-400">
                                        <span class="w-1.5 h-1.5 rounded-full bg-${{statusColor}}-400 ${{pulseAnimation}}"></span>
                                        ${{status}}
                                    </span>
                                    <span class="text-gray-500 text-[10px] tracking-wide uppercase mt-1">${{responseTime}}</span>
                                </div>
                            </div>
                            <div class="mt-6 pt-3 border-t border-gray-800/60 flex justify-between items-center w-full">
                                <span class="text-[10px] font-mono text-gray-500 tracking-wider">NODE ID: #${{w.id}}</span>
                                <a href="/delete-website/${{w.id}}" class="text-red-400 hover:text-red-300 text-xs font-medium inline-flex items-center gap-1 transition"><i class="fas fa-trash-can text-[10px]"></i> Terminate Node</a>
                            </div>
                        </div>
                    `;
                }}
                container.innerHTML = newCards;
            }} catch (err) {{
                console.error('Status refresh execution error:', err);
            }}
        }}
        setInterval(refreshStatus, 60000);
        document.addEventListener('DOMContentLoaded', refreshStatus);
    </script>
</body>
</html>'''
    return template.replace("__USER_EMAIL__", current_user.email)\
                   .replace("__PRO_BADGE__", pro_badge)\
                   .replace("__FLASH_MESSAGES__", flash_messages)\
                   .replace("__CARDS__", cards if cards else '<div class="col-span-full bg-gray-900/20 border border-dashed border-gray-800 rounded-2xl py-12 text-center text-sm text-gray-500 flex flex-col items-center justify-center gap-2"><i class="fas fa-folder-open text-xl opacity-40"></i> No infrastructure nodes configured to this cluster environment index.</div>')

@app.route("/add-website", methods=["GET", "POST"])
@login_required
def add_website():
    if request.method == "POST":
        name = request.form["name"].strip()
        url = request.form["url"].strip().rstrip('/')
        existing = Website.query.filter_by(user_id=current_user.id, url=url).first()
        if existing:
            flash(f"This URL ({url}) is already actively monitored!")
            return redirect(url_for("dashboard"))
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Pulse | Mount Cluster Target</title><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="mb-6"><h2 class="text-xl font-bold text-white tracking-tight">Provision Monitoring Node</h2><p class="text-xs text-gray-400 mt-1">Bind a remote HTTP endpoint asset.</p></div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Asset Identification Title</label><input name="name" placeholder="Production Server" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Target Absolute URL Path</label><input name="url" type="url" placeholder="https://api.domain.com" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" required></div>
            <div class="flex gap-3 pt-2"><a href="/dashboard" class="w-1/3 text-center border border-gray-800 hover:bg-gray-900 text-gray-400 py-3 rounded-xl text-sm font-semibold transition">Abort</a><button type="submit" class="w-2/3 bg-white hover:bg-gray-200 text-gray-950 py-3 rounded-xl text-sm font-semibold shadow-lg transition active:scale-[0.99]">Mount Node</button></div>
        </form>
    </div>
</body>
</html>
    '''

@app.route("/delete-website/<int:website_id>")
@login_required
def delete_website(website_id):
    try:
        website = db.session.get(Website, website_id)
        if website and website.user_id == current_user.id:
            db.session.delete(website)
            db.session.commit()
            flash("Website removed.")
        else:
            flash("Unauthorized or not found.")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Delete error: {e}")
        flash("System error during deletion.")
    return redirect(url_for("dashboard"))

# ---------- PAYSTACK UPGRADE ----------
@app.route("/upgrade")
@login_required
def upgrade():
    if not PAYSTACK_SECRET_KEY:
        return "Paystack secret key missing", 500
    amount_kobo = 1500000
    ref = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    data = {"amount": amount_kobo, "email": current_user.email, "reference": ref, "callback_url": url_for("payment_success", _external=True)}
    try:
        r = requests.post("https://api.paystack.co/transaction/initialize", json=data, headers=headers)
        result = r.json()
        if result.get("status"):
            return redirect(result["data"]["authorization_url"])
        flash("Paystack initialization failed.")
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Error: {e}")
        return redirect(url_for("dashboard"))

@app.route("/payment-success")
def payment_success():
    ref = request.args.get("reference")
    if not ref:
        flash("Missing reference")
        return redirect(url_for("dashboard"))
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
                flash("Upgrade complete! You are now Pro.")
            else:
                flash("User not found.")
        else:
            flash("Verification failed.")
    except Exception:
        flash("Error verifying payment.")
    return redirect(url_for("dashboard"))

# ---------- BACKGROUND CHECKS ----------
@app.route("/update-all")
def update_all():
    lock_file = open("/tmp/update_automation.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return "Already running", 429
    try:
        website_ids = [w.id for w in Website.query.all()]
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(process_single_website, website_ids)
        return "OK", 200
    except Exception as e:
        app.logger.error(f"Update-all error: {e}")
        return "ERR", 500
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

@app.route("/ping")
def ping():
    return "OK", 200

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
