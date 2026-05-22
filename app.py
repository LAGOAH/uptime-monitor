from flask import Flask, redirect, url_for, request, flash, get_flashed_messages, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
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

# 1. OPTIMIZATION: Strengthened Production Secret Key Setup
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-only-in-local")

# Database Setup & Configuration
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uptime.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# 2. OPTIMIZATION: High-Concurrency Connection Pooling Options
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
        if 200 <= r.status_code < 400:
            return "UP", rt
        else:
            return "DOWN", rt
    except Exception:
        return "DOWN", None

# 3. OPTIMIZATION: Email Rate-Limit Aware Retry Loop Block
def send_alert_email(user_email, website_name, status, url, response_time="N/A"):
    if not resend.api_key:
        app.logger.warning("Resend API key missing – email aborted")
        return False
        
    time.sleep(0.3)  # Gentle structural delay to smooth overlapping concurrent thread requests
    subject = f"⚠️ Alert: {website_name} is {status}"
    html = f"""
    <div style="background-color:#0b0f19; color:#f3f4f6; padding:24px; font-family:sans-serif; border-radius:12px;">
        <h2 style="color:#6366f1;">📡 Uptime Monitor Notification</h2>
        <p>Your website <strong>{website_name}</strong> (<a href="{url}" style="color:#818cf8;">{url}</a>) status shifted to <strong style="color:{'#10b981' if status=='UP' else '#ef4444'};">{status}</strong>.</p>
        <p><strong>Latency Telemetry:</strong> {response_time} seconds</p>
        <p style="color:#9ca3af; font-size:12px;">Event log timestamps sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
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
            app.logger.info(f"Notification email dispatched to {user_email}")
            return True
        except Exception as e:
            app.logger.error(f"Email delivery attempt {attempt + 1} failed: {e}")
            if "429" in str(e) or "rate" in str(e).lower():
                time.sleep(1.5 * (attempt + 1))  # Exponential backoff on rate-limiting
            else:
                time.sleep(0.5)
    return False

def process_single_website(website):
    with app.app_context():
        try:
            user = db.session.get(User, website.user_id)
            if not user:
                return
            
            current_status, response_time = check_website(website.url)
            
            # Persist Latency Metric Row into Database History Logs
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
            app.logger.error(f"Exception scanning targets for {website.url}: {e}")

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
    <title>📡 Pulse - Next-Gen Autonomous Uptime Monitoring</title>
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
                <a href="/login" class="text-sm font-medium text-gray-400 hover:text-white transition px-3 py-1.5 rounded-lg hover:bg-gray-900">Log in</a>
                <a href="/signup" class="text-sm font-medium bg-white text-gray-950 px-4 py-2 rounded-xl hover:bg-gray-200 transition shadow-[0_4px_20px_rgba(255,255,255,0.15)] transform active:scale-95">Sign up free</a>
            </div>
        </div>
    </nav>

    <section class="relative max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 pt-24 pb-16 text-center z-10">
        <div class="inline-flex items-center gap-2 bg-indigo-500/10 border border-indigo-500/30 rounded-full px-3.5 py-1.5 mb-6 transform hover:scale-105 transition-all cursor-pointer">
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
            <a href="#pricing" class="w-full sm:w-auto border border-gray-800 bg-gray-900/40 hover:bg-gray-900 text-gray-300 px-8 py-4 rounded-xl font-semibold transition">
                Explore Analytics
            </a>
        </div>
    </section>

    <section class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition">
                <div class="w-10 h-10 rounded-lg bg-indigo-500/10 flex items-center justify-center text-indigo-400 mb-4"><i class="fas fa-bolt"></i></div>
                <h3 class="text-lg font-bold text-white mb-2">High-Velocity Scans</h3>
                <p class="text-sm text-gray-400 leading-relaxed">Parallel engine blocks cycle every 60 seconds natively to isolate outages instantaneously.</p>
            </div>
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition">
                <div class="w-10 h-10 rounded-lg bg-purple-500/10 flex items-center justify-center text-purple-400 mb-4"><i class="fas fa-bell"></i></div>
                <h3 class="text-lg font-bold text-white mb-2">Instant Escalations</h3>
                <p class="text-sm text-gray-400 leading-relaxed">Direct routing loops into Resend API transactional channels to bypass standard inbox delay traps.</p>
            </div>
            <div class="bg-gray-900/40 border border-gray-800/80 rounded-2xl p-6 backdrop-blur-sm shadow-xl hover:border-gray-700 transition">
                <div class="w-10 h-10 rounded-lg bg-pink-500/10 flex items-center justify-center text-pink-400 mb-4"><i class="fas fa-chart-bar"></i></div>
                <h3 class="text-lg font-bold text-white mb-2">Granular Latency Logs</h3>
                <p class="text-sm text-gray-400 leading-relaxed">Capture precise telemetry tracking metrics across execution runs to preserve analytical records.</p>
            </div>
        </div>
    </section>

    <section id="pricing" class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 relative z-10">
        <div class="text-center mb-12">
            <h2 class="text-3xl sm:text-5xl font-extrabold text-white mb-3">Predictable pricing paradigms</h2>
            <p class="text-gray-400 text-sm sm:text-base">Scale your visibility bounds without micro-transaction penalties.</p>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-4xl mx-auto items-stretch">
            <div class="bg-gray-900/30 border border-gray-800/80 rounded-3xl p-8 flex flex-col justify-between backdrop-blur-md shadow-2xl relative overflow-hidden group">
                <div>
                    <h3 class="text-xl font-bold text-white mb-1">Standard Node</h3>
                    <p class="text-xs text-gray-500 mb-6">Perfect for static personal landing deployments.</p>
                    <div class="flex items-baseline gap-1.5 mb-6">
                        <span class="text-4xl font-extrabold text-white">₦0</span>
                        <span class="text-xs text-gray-500">/ forever</span>
                    </div>
                    <ul class="space-y-4 border-t border-gray-800/80 pt-6">
                        <li class="flex items-center gap-3 text-sm text-gray-300"><i class="fas fa-circle-check text-indigo-500 text-xs"></i> 3 Monitored Endpoints</li>
                        <li class="flex items-center gap-3 text-sm text-gray-300"><i class="fas fa-circle-check text-indigo-500 text-xs"></i> Standard Email Triggers</li>
                        <li class="flex items-center gap-3 text-sm text-gray-400 opacity-50"><i class="fas fa-circle-xmark text-gray-600 text-xs"></i> 1-Minute Premium Checks</li>
                    </ul>
                </div>
                <a href="/signup" class="block w-full text-center bg-gray-800 hover:bg-gray-700 text-white font-medium text-sm py-3 rounded-xl mt-8 transition">Deploy Free Cluster</a>
            </div>

            <div class="bg-gradient-to-b from-indigo-950/40 to-gray-950/40 border-2 border-indigo-500/80 rounded-3xl p-8 flex flex-col justify-between backdrop-blur-md shadow-2xl relative overflow-hidden group shadow-[0_0_50px_-12px_rgba(99,102,241,0.3)]">
                <div class="absolute top-0 right-0 bg-indigo-500 text-white font-bold tracking-wider text-[10px] uppercase px-4 py-1 rounded-bl-xl shadow-md">Premium Tier</div>
                <div>
                    <h3 class="text-xl font-bold text-white mb-1">Enterprise Pro</h3>
                    <p class="text-xs text-indigo-300 mb-6">For real-time operational scale applications.</p>
                    <div class="flex items-baseline gap-1.5 mb-6">
                        <span class="text-4xl font-extrabold text-white">₦15,000</span>
                        <span class="text-xs text-gray-400">/ month</span>
                    </div>
                    <ul class="space-y-4 border-t border-indigo-900/50 pt-6">
                        <li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> Unlimited Registered Endpoints</li>
                        <li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> High-Speed 1-Minute Diagnostics</li>
                        <li class="flex items-center gap-3 text-sm text-gray-200"><i class="fas fa-circle-check text-indigo-400 text-xs"></i> Detailed Latency History Logging</li>
                    </ul>
                </div>
                <a href="/signup" class="block w-full text-center bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-sm py-3 rounded-xl mt-8 shadow-lg shadow-indigo-600/30 transition">Provision Pro Engine</a>
            </div>
        </div>
    </section>

    <footer class="border-t border-gray-800/80 bg-gray-950/40 py-8 relative z-10">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row justify-between items-center gap-4 text-center sm:text-left">
            <span class="text-xs text-gray-500">© 2026 Pulse Systems Inc. All rights reserved. Engineered for performance.</span>
            <div class="flex gap-4 text-gray-500 text-sm">
                <a href="#" class="hover:text-white transition"><i class="fab fa-github"></i></a>
                <a href="#" class="hover:text-white transition"><i class="fab fa-twitter"></i></a>
            </div>
        </div>
    </footer>
</body>
</html>
    '''

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        if User.query.filter_by(email=email).first():
            flash("Email already registered")
            return redirect(url_for("signup"))
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard"))
    return '''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Sign up</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6">
            <h2 class="text-2xl font-bold text-white tracking-tight">Create your account</h2>
            <p class="text-xs text-gray-400 mt-1">Instant telemetry routing initialization.</p>
        </div>
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
        email = request.form["email"]
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Invalid email or password")
    return '''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Login</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="text-center mb-6">
            <h2 class="text-2xl font-bold text-white tracking-tight">Access Control Interface</h2>
            <p class="text-xs text-gray-400 mt-1">Authenticate access protocols.</p>
        </div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Email Address</label><input name="email" type="email" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="you@domain.com" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Password</label><input name="password" type="password" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" placeholder="••••••••" required></div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-xl text-sm font-semibold shadow-lg shadow-indigo-600/20 transition active:scale-[0.99]">Establish Authentication</button>
        </form>
        <p class="text-xs text-center text-gray-500 mt-4">Missing authorization? <a href="/signup" class="text-indigo-400 hover:underline">Register cluster node</a></p>
    </div>
</body>
</html>
    '''

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

# ---------- SILENT BACKGROUND LIVE CHECK ENDPOINT ----------
@app.route("/api/status")
@login_required
def api_status():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    data = []
    for w in websites:
        status, rt = check_website(w.url)
        data.append({
            "id": w.id,
            "name": w.name,
            "url": w.url,
            "status": status,
            "response_time": rt if rt else "N/A"
        })
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
                <a href="/delete-website/{w.id}" class="text-red-400 hover:text-red-300 text-xs font-medium inline-flex items-center gap-1 transition">
                    <i class="fas fa-trash-can text-[10px]"></i> Terminate Node
                </a>
            </div>
        </div>
        '''
    
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div class="bg-indigo-950/60 border border-indigo-500/30 text-indigo-300 text-xs px-4 py-3 rounded-xl mb-6 shadow-lg flex items-center gap-2"><i class="fas fa-circle-info"></i> SYSTEM: {msg}</div>'
    
    pro_badge = '<span class="bg-gradient-to-r from-amber-400 to-yellow-500 text-gray-950 px-3 py-1 rounded-full text-xs font-extrabold tracking-tight shadow-md shadow-yellow-500/10">⭐ PRO ACTIVE</span>' if current_user.is_pro else '<a href="/upgrade" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-full text-xs font-bold shadow-md shadow-indigo-600/20 transition active:scale-95">Upgrade to Pro</a>'
    
    return f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Analytics Console</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
        body {{ font-family: 'Plus Jakarta Sans', sans-serif; background-color: #030712; }}
    </style>
</head>
<body class="text-gray-100 selection:bg-indigo-500/30 overflow-x-hidden">
    <div class="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-7xl h-[400px] pointer-events-none opacity-20 blur-[120px] bg-indigo-600 rounded-full top-[-200px]"></div>

    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-10 relative z-10">
        <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 border-b border-gray-800/80 pb-6 mb-8">
            <div class="min-w-0">
                <span class="text-[10px] font-mono tracking-widest uppercase text-indigo-400">OPERATIONAL CONTROL UNIT</span>
                <h1 class="text-xl sm:text-3xl font-extrabold text-white tracking-tight truncate mt-0.5">Instance: {current_user.email}</h1>
                <div class="mt-2">{pro_badge}</div>
            </div>
            <div class="flex items-center gap-2.5 w-full sm:w-auto shrink-0">
                <a href="/add-website" class="flex-1 sm:flex-initial text-center bg-white hover:bg-gray-200 text-gray-950 px-4 py-2.5 rounded-xl text-sm font-semibold transition shadow-md shadow-white/5 active:scale-[0.98]">+ Inject Node</a>
                <a href="/logout" class="bg-gray-900 border border-gray-800 hover:bg-gray-800 text-gray-400 px-4 py-2.5 rounded-xl text-sm font-semibold transition active:scale-[0.98]"><i class="fas fa-right-from-bracket"></i></a>
            </div>
        </div>
        
        <div id="flash-container">{flash_messages}</div>

        <div id="cards-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {cards if cards else '<div class="col-span-full bg-gray-900/20 border border-dashed border-gray-800 rounded-2xl py-12 text-center text-sm text-gray-500 flex flex-col items-center justify-center gap-2"><i class="fas fa-folder-open text-xl opacity-40"></i> No infrastructure nodes configured to this cluster environment index.</div>'}
        </div>
    </div>

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
                                    <span class="inline-flex items-center gap-1.5 bg-\${statusBg} border border-\${statusBorder} px-2.5 py-1 rounded-full text-xs font-semibold text-\${statusColor}-400">
                                        <span class="w-1.5 h-1.5 rounded-full bg-\${statusColor}-400 \${pulseAnimation}"></span>
                                        \${status}
                                    </span>
                                    <span class="text-gray-500 text-[10px] tracking-wide uppercase mt-1">\${responseTime}</span>
                                </div>
                            </div>
                            <div class="mt-6 pt-3 border-t border-gray-800/60 flex justify-between items-center w-full">
                                <span class="text-[10px] font-mono text-gray-500 tracking-wider">NODE ID: #\${w.id}</span>
                                <a href="/delete-website/\${w.id}" class="text-red-400 hover:text-red-300 text-xs font-medium inline-flex items-center gap-1 transition">
                                    <i class="fas fa-trash-can text-[10px]"></i> Terminate Node
                                </a>
                            </div>
                        </div>
                    `;
                }}
                container.innerHTML = newCards;
            }} catch (err) {{
                console.error('Status refresh execution error:', err);
            }}
        }}
        
        // Polling loop updates components every 15 seconds safely
        setInterval(refreshStatus, 15000);
    </script>
</body>
</html>
    '''

@app.route("/add-website", methods=["GET", "POST"])
@login_required
def add_website():
    if request.method == "POST":
        name = request.form["name"]
        url = request.form["url"]
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Inject Node</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-[#030712] text-gray-100 flex items-center justify-center min-h-screen px-4">
    <div class="absolute inset-0 max-w-md mx-auto h-[400px] blur-[120px] bg-indigo-600/20 top-1/4 rounded-full pointer-events-none"></div>
    <div class="bg-gray-900/50 border border-gray-800/80 p-6 sm:p-8 rounded-2xl shadow-2xl w-full max-w-md backdrop-blur-md relative z-10">
        <div class="mb-6">
            <h2 class="text-xl font-bold text-white tracking-tight">Provision Monitoring Node</h2>
            <p class="text-xs text-gray-400 mt-1">Bind a remote HTTP endpoint asset.</p>
        </div>
        <form method="post" class="space-y-4">
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Asset Identification Title</label><input name="name" placeholder="Production Server" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" required></div>
            <div><label class="text-xs text-gray-400 font-medium block mb-1.5">Target Absolute URL Path</label><input name="url" type="url" placeholder="https://api.domain.com" class="w-full bg-gray-950/80 border border-gray-800 rounded-xl p-3 text-sm focus:border-indigo-500 focus:outline-none transition text-white" required></div>
            <div class="flex gap-3 pt-2">
                <a href="/dashboard" class="w-1/3 text-center border border-gray-800 hover:bg-gray-900 text-gray-400 py-3 rounded-xl text-sm font-semibold transition">Abort</a>
                <button type="submit" class="w-2/3 bg-white hover:bg-gray-200 text-gray-950 py-3 rounded-xl text-sm font-semibold shadow-lg transition active:scale-[0.99]">Mount Node</button>
            </div>
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
        if not website or website.user_id != current_user.id:
            flash("Unauthorized or missing target.")
            return redirect(url_for("dashboard"))
        
        db.session.delete(website)
        db.session.commit()
        flash("Website removed.")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed removal sequence transaction: {e}")
        flash("System transaction failed.")
    return redirect(url_for("dashboard"))

# ---------- PRODUCTION PAYSTACK PAYMENT INTEGRATION ----------
@app.route("/upgrade")
@login_required
def upgrade():
    if not PAYSTACK_SECRET_KEY:
        return "Paystack secret key configurations missing on server environment", 500
    amount_kobo = 1500000  # ₦15,000.00
    ref = secrets.token_hex(16)
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    data = {
        "amount": amount_kobo,
        "email": current_user.email,
        "reference": ref,
        "callback_url": url_for("payment_success", _external=True),
    }
    try:
        r = requests.post("https://api.paystack.co/transaction/initialize", json=data, headers=headers)
        result = r.json()
        if result.get("status"):
            return redirect(result["data"]["authorization_url"])
        flash("Paystack could not initialize session.")
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Gateway connection error: {e}")
        return redirect(url_for("dashboard"))

@app.route("/payment-success")
def payment_success():
    ref = request.args.get("reference")
    if not ref:
        flash("Verification token sequence missing.")
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
                flash("Upgrade complete! Your account is now active on the Pro tier.")
            else:
                flash("Transaction successful, but match account context lost.")
        else:
            flash("Payment authentication rejected by Paystack.")
    except Exception:
        flash("Verification process met an external fault.")
    return redirect(url_for("dashboard"))

# ---------- PARALLEL BG ENGINE BLOCK (WITH LOCK PROTECTION) ----------
@app.route("/update-all")
def update_all():
    # File-locking protects concurrent threads against overlapping cron triggers
    lock_file = open("/tmp/update_automation.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        app.logger.warning("Overlapping cron execution blocked safely.")
        lock_file.close()
        return "Already running", 429

    try:
        websites = Website.query.all()
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(process_single_website, websites)
            
        return "OK", 200
    except Exception as e:
        app.logger.error(f"Background automation framework error: {e}")
        return "ERR", 500
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

with app.app_context():
    db.create_all()

@app.route("/ping")
def ping():
    return "OK", 200

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
