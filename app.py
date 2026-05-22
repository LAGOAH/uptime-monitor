from flask import Flask, redirect, url_for, request, flash, get_flashed_messages
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
from datetime import datetime, timezone
import secrets
import resend
import logging
import fcntl
from concurrent.futures import ThreadPoolExecutor

# Configure production-ready standard logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

app = Flask(__name__)
# Pull secret key from production environments safely
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

# Database Setup
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uptime.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# Third-Party API Hooks
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
    
    # Cascade configuration safely cleans up downstream data upon deletion
    alerts = db.relationship("Alert", backref="website", cascade="all, delete-orphan", lazy=True)
    history = db.relationship("ResponseHistory", backref="website", cascade="all, delete-orphan", lazy=True)

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    website_id = db.Column(db.Integer, db.ForeignKey("website.id"), nullable=False)
    last_status = db.Column(db.String(10), nullable=False)
    last_alert_sent = db.Column(db.DateTime, nullable=True)

# New Table providing historical response-time logs for Pro Tier Analytics
class ResponseHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    website_id = db.Column(db.Integer, db.ForeignKey("website.id"), nullable=False)
    status = db.Column(db.String(10), nullable=False)
    response_time = db.Column(db.Float, nullable=True) # Stored in seconds (e.g., 0.241)
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

def send_alert_email(user_email, website_name, status, url):
    if not resend.api_key:
        app.logger.warning("Resend API key missing – alert drop notification execution aborted")
        return
    subject = f"⚠️ Alert: {website_name} is {status}"
    html = f"""
    <p>Your website <strong>{website_name}</strong> ({url}) is now <strong>{status}</strong>.</p>
    <p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><a href="https://uptime-monitor-q3hi.onrender.com/dashboard">View dashboard</a></p>
    """
    try:
        resend.Emails.send({
            "from": DEFAULT_FROM_EMAIL,
            "to": [user_email],
            "subject": subject,
            "html": html,
        })
        app.logger.info(f"Notification email dispatched to {user_email} for {website_name} ({status})")
    except Exception as e:
        app.logger.error(f"Failed dispatching mail communication context via Resend nodes: {e}")

# Process a single website check inside an isolated thread context safely
def process_single_website(website):
    with app.app_context():
        try:
            user = db.session.get(User, website.user_id)
            if not user:
                return
            
            current_status, response_time = check_website(website.url)
            
            # Log metric parameters across user accounts to satisfy 'Response Time History'
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
                send_alert_email(user.email, website.name, current_status, website.url)
                alert.last_status = current_status
                alert.last_alert_sent = datetime.now()
                
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Exception handling trace parameters for remote client index URL target {website.url}: {e}")

# ---------- ROUTES ----------

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Uptime Monitor – Never miss downtime</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>@import url('https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,400;14..32,500;14..32,600;14..32,700&display=swap');body{font-family:'Inter',sans-serif;}.gradient-bg{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);}</style>
</head>
<body class="bg-gray-50">
    <nav class="bg-white shadow-sm"><div class="max-w-7xl mx-auto px-4 py-4 flex justify-between items-center"><div class="text-xl font-bold text-indigo-600">📡 Uptime Monitor</div><div class="space-x-4"><a href="/login" class="text-gray-600 hover:text-indigo-600 transition">Log in</a><a href="/signup" class="bg-indigo-600 text-white px-4 py-2 rounded-lg hover:bg-indigo-700 transition shadow-sm">Sign up free</a></div></div></nav>
    <section class="max-w-7xl mx-auto px-4 py-20 text-center"><h1 class="text-4xl md:text-6xl font-extrabold text-gray-900">Know the second your <span class="text-indigo-600">website goes down</span></h1><p class="mt-6 text-xl text-gray-500 max-w-3xl mx-auto">Get instant alerts via email. Free plan monitors 3 websites. Upgrade to Pro for sub-minute check operations.</p><div class="mt-10 flex flex-col sm:flex-row justify-center gap-4"><a href="/signup" class="bg-indigo-600 text-white px-8 py-3 rounded-xl text-lg font-semibold shadow-lg hover:bg-indigo-700 transition transform hover:scale-105">Start monitoring for free →</a><a href="#pricing" class="border border-indigo-600 text-indigo-600 px-8 py-3 rounded-xl text-lg font-semibold hover:bg-indigo-50 transition">See pricing</a></div></section>
    <section id="pricing" class="py-20 bg-gray-50"><div class="max-w-7xl mx-auto px-4"><div class="text-center mb-12"><h2 class="text-3xl md:text-4xl font-bold text-gray-900">Simple, transparent pricing</h2></div><div class="grid md:grid-cols-2 gap-8 max-w-4xl mx-auto"><div class="bg-white rounded-2xl shadow-lg border border-gray-200 p-8"><h3 class="text-2xl font-bold">Free</h3><div class="mt-4"><span class="text-4xl font-bold">₦0</span> <span class="text-gray-500">/ month</span></div><ul class="mt-6 space-y-3"><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Monitor up to 3 websites</li><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Email alerts</li><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Standard 5‑minute checks</li></ul><div class="mt-8"><a href="/signup" class="block text-center bg-indigo-600 text-white py-2 rounded-xl">Get started</a></div></div>
    <div class="bg-white rounded-2xl shadow-xl border-2 border-indigo-500 transform scale-105"><div class="bg-indigo-500 text-white text-center py-2 text-sm font-semibold">MOST POPULAR</div><div class="p-8"><h3 class="text-2xl font-bold">Pro</h3><div class="mt-4"><span class="text-4xl font-bold">₦15,000</span> <span class="text-gray-500">/ month</span></div><ul class="mt-6 space-y-3"><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Unlimited websites</li><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Priority email alerts</li><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>High-speed 1‑minute checks</li><li class="flex items-center"><i class="fas fa-check-circle text-green-500 mr-2"></i>Response time history</li></ul><div class="mt-8"><a href="/signup" class="block text-center bg-indigo-600 text-white py-2 rounded-xl">Start Pro Tier</a></div></div></div></div></div></section>
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
<html>
<head><meta charset="UTF-8"><title>Sign up</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50 flex items-center justify-center min-h-screen">
    <div class="bg-white p-8 rounded-2xl shadow-xl w-full max-w-md">
        <h2 class="text-3xl font-bold text-center text-indigo-600 mb-6">Create account</h2>
        <form method="post">
            <input name="email" type="email" placeholder="Email" class="w-full border p-3 rounded-lg mb-4" required>
            <input name="password" type="password" placeholder="Password" class="w-full border p-3 rounded-lg mb-6" required>
            <button type="submit" class="w-full bg-indigo-600 text-white py-3 rounded-lg font-semibold">Sign up</button>
        </form>
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
<html>
<head><meta charset="UTF-8"><title>Login</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50 flex items-center justify-center min-h-screen">
    <div class="bg-white p-8 rounded-2xl shadow-xl w-full max-w-md">
        <h2 class="text-3xl font-bold text-center text-indigo-600 mb-6">Welcome back</h2>
        <form method="post">
            <input name="email" type="email" placeholder="Email" class="w-full border p-3 rounded-lg mb-4" required>
            <input name="password" type="password" placeholder="Password" class="w-full border p-3 rounded-lg mb-6" required>
            <button type="submit" class="w-full bg-indigo-600 text-white py-3 rounded-lg font-semibold">Login</button>
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

@app.route("/dashboard")
@login_required
def dashboard():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    cards = ""
    for w in websites:
        status, rt = check_website(w.url)
        status_color = "green" if status == "UP" else "red"
        status_icon = "✅" if status == "UP" else "❌"
        cards += f'''
        <div class="bg-white rounded-xl shadow-md p-5 hover:shadow-xl transition-all">
            <div class="flex justify-between items-start">
                <div>
                    <h3 class="font-bold text-lg">{w.name}</h3>
                    <p class="text-gray-500 text-sm truncate max-w-[200px]">{w.url}</p>
                </div>
                <div class="text-right">
                    <span class="text-{status_color}-600 font-semibold flex items-center gap-1">{status_icon} {status}</span>
                    <span class="text-gray-400 text-xs">{rt if rt else 'N/A'} s</span>
                </div>
            </div>
            <div class="mt-4 flex justify-end">
                <a href="/delete-website/{w.id}" class="text-red-500 hover:text-red-700 text-sm transition">🗑️ Delete</a>
            </div>
        </div>
        '''
    
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div class="bg-indigo-100 text-indigo-700 px-4 py-2 rounded mb-4">⚠️ {msg}</div>'
    
    pro_badge = '<span class="bg-yellow-400 text-black px-3 py-1 rounded-full text-sm font-bold">⭐ Pro</span>' if current_user.is_pro else '<a href="/upgrade" class="bg-green-600 text-white px-3 py-1 rounded-full text-sm">Upgrade to Pro</a>'
    
    return f'''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta http-equiv="refresh" content="60"><title>Dashboard</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50">
    <div class="max-w-6xl mx-auto px-4 py-8">
        <div class="flex justify-between items-center mb-8">
            <div>
                <h1 class="text-3xl font-bold text-gray-800">Welcome, {current_user.email} {pro_badge}</h1>
            </div>
            <div class="space-x-3">
                <a href="/add-website" class="bg-indigo-600 text-white px-5 py-2 rounded-lg">+ Add Website</a>
                <a href="/logout" class="bg-gray-200 text-gray-700 px-5 py-2 rounded-lg">Logout</a>
            </div>
        </div>
        {flash_messages}
        <div class="grid md:grid-cols-2 lg:grid-cols-3 gap-6">{cards if cards else 'No websites added.'}</div>
    </div>
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
<html>
<head><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-50 flex items-center justify-center min-h-screen">
    <div class="bg-white p-8 rounded-2xl shadow-xl w-full max-w-md">
        <h2 class="text-2xl font-bold mb-6">Add Website</h2>
        <form method="post">
            <input name="name" placeholder="Site name" class="w-full border p-3 rounded-lg mb-4" required>
            <input name="url" placeholder="https://..." class="w-full border p-3 rounded-lg mb-6" required>
            <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded-lg">Add</button>
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
        
        # Cascades on structural relations clean Alerts and Histories automatically
        db.session.delete(website)
        db.session.commit()
        flash("Website removed.")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed removal sequence transaction on user assets: {e}")
        flash("System transaction failed.")
    return redirect(url_for("dashboard"))

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

# ---------- PARALLEL BACKGROUND MONITORING (WITH LOCK PROTECTION) ----------
@app.route("/update-all")
def update_all():
    # Attempt to establish an exclusive, non-blocking file lock to prevent overlapping runs
    lock_file = open("/tmp/update_automation.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        app.logger.warning("Overlapping cron job execution blocked. Existing /update-all worker is active.")
        lock_file.close()
        return "Already running", 429

    try:
        websites = Website.query.all()
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(process_single_website, websites)
            
        return "OK", 200
    except Exception as e:
        app.logger.error(f"Background automation framework encountered a processing fault: {e}")
        return "ERR", 500
    finally:
        # Guarantee lock release upon loop lifecycle closure
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

with app.app_context():
    db.create_all()

@app.route("/ping")
def ping():
    return "OK", 200

if __name__ == "__main__":
    # Dynamically pull application execution context safely matching local/production variables
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
