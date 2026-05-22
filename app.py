from flask import Flask, redirect, url_for, request, flash, get_flashed_messages
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
from datetime import datetime
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

# Database setup
database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///uptime.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# Paystack key
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")

# User model
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

# Website model
class Website(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

# Alert tracking
class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    website_id = db.Column(db.Integer, db.ForeignKey("website.id"), nullable=False)
    last_status = db.Column(db.String(10), nullable=False)
    last_alert_sent = db.Column(db.DateTime, nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def check_website(url):
    try:
        start = datetime.now()
        r = requests.get(url, timeout=5)
        response_time = (datetime.now() - start).total_seconds()
        if 200 <= r.status_code < 400:
            return "UP", round(response_time, 3)
        else:
            return "DOWN", round(response_time, 3)
    except:
        return "DOWN", None

def send_alert_email(user_email, website_name, status, url):
    # You can replace with Resend later – for now just print
    print(f"📧 Alert: {website_name} is {status} for {user_email}")

# ---------- LANDING PAGE ----------
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
    <title>Uptime Monitor – Never miss a downtime</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,400;14..32,500;14..32,600;14..32,700&display=swap');
        body { font-family: 'Inter', sans-serif; }
        .gradient-bg { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
        .pulse { animation: pulse 2s infinite; }
        @keyframes pulse { 0% { transform: scale(1); opacity: 0.7; } 70% { transform: scale(1.05); opacity: 0.4; } 100% { transform: scale(1); opacity: 0.7; } }
    </style>
</head>
<body class="bg-gray-50">
    <nav class="bg-white shadow-sm">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 flex justify-between items-center">
            <div class="text-xl font-bold text-indigo-600">📡 Uptime Monitor</div>
            <div class="space-x-4">
                <a href="/login" class="text-gray-600 hover:text-indigo-600 transition">Log in</a>
                <a href="/signup" class="bg-indigo-600 text-white px-4 py-2 rounded-lg hover:bg-indigo-700 transition shadow-sm">Sign up free</a>
            </div>
        </div>
    </nav>
    <section class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 text-center">
        <h1 class="text-4xl md:text-6xl font-extrabold text-gray-900 tracking-tight">Know the second your <span class="text-indigo-600">website goes down</span></h1>
        <p class="mt-6 text-xl text-gray-500 max-w-3xl mx-auto">Get instant alerts via email. Free plan monitors 3 websites. Upgrade to Pro for unlimited monitoring – no hidden fees.</p>
        <div class="mt-10 flex flex-col sm:flex-row justify-center gap-4">
            <a href="/signup" class="bg-indigo-600 text-white px-8 py-3 rounded-xl text-lg font-semibold shadow-lg hover:bg-indigo-700 transition transform hover:scale-105">Start monitoring for free →</a>
            <a href="#pricing" class="border border-indigo-600 text-indigo-600 px-8 py-3 rounded-xl text-lg font-semibold hover:bg-indigo-50 transition">See pricing</a>
        </div>
        <div class="mt-16 relative">
            <div class="absolute inset-0 flex items-center justify-center opacity-20"><div class="w-64 h-64 bg-indigo-300 rounded-full blur-3xl pulse"></div></div>
            <div class="bg-white rounded-2xl shadow-2xl overflow-hidden border border-gray-200 max-w-4xl mx-auto">
                <div class="bg-gray-100 px-4 py-2 border-b flex items-center space-x-2">
                    <div class="w-3 h-3 rounded-full bg-red-400"></div><div class="w-3 h-3 rounded-full bg-yellow-400"></div><div class="w-3 h-3 rounded-full bg-green-400"></div>
                    <div class="text-sm text-gray-500 ml-2">dashboard.uptime-monitor.com</div>
                </div>
                <div class="p-6">
                    <div class="flex justify-between items-center mb-6"><h2 class="text-xl font-bold">Your websites</h2><button class="bg-green-500 text-white px-4 py-2 rounded-lg text-sm">+ Add website</button></div>
                    <div class="space-y-3">
                        <div class="flex justify-between items-center p-3 bg-gray-50 rounded-lg"><span class="font-medium">Google</span><span class="text-green-600 font-semibold">UP <span class="text-gray-400 text-sm ml-2">0.24s</span></span></div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 rounded-lg"><span class="font-medium">GitHub</span><span class="text-green-600 font-semibold">UP <span class="text-gray-400 text-sm ml-2">0.31s</span></span></div>
                        <div class="flex justify-between items-center p-3 bg-gray-50 rounded-lg"><span class="font-medium">YourSite.com</span><span class="text-red-500 font-semibold">DOWN</span></div>
                    </div>
                </div>
            </div>
        </div>
    </section>
    <section class="bg-white py-20">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center mb-12"><h2 class="text-3xl md:text-4xl font-bold text-gray-900">Everything you need</h2><p class="text-gray-500 mt-2">Simple, reliable, and built for developers and businesses</p></div>
            <div class="grid md:grid-cols-3 gap-8">
                <div class="bg-gray-50 p-6 rounded-xl text-center hover:shadow-md transition"><i class="fas fa-globe text-indigo-600 text-4xl mb-4"></i><h3 class="text-xl font-semibold">Monitor from anywhere</h3><p class="text-gray-500 mt-2">Check your websites every 5 minutes from global nodes.</p></div>
                <div class="bg-gray-50 p-6 rounded-xl text-center hover:shadow-md transition"><i class="fas fa-envelope text-indigo-600 text-4xl mb-4"></i><h3 class="text-xl font-semibold">Instant email alerts</h3><p class="text-gray-500 mt-2">Get notified immediately when a site goes down or recovers.</p></div>
                <div class="bg-gray-50 p-6 rounded-xl text-center hover:shadow-md transition"><i class="fas fa-chart-line text-indigo-600 text-4xl mb-4"></i><h3 class="text-xl font-semibold">Response time tracking</h3><p class="text-gray-500 mt-2">See exactly how fast your websites load over time.</p></div>
            </div>
        </div>
    </section>
    <section id="pricing" class="py-20 bg-gray-50">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center mb-12"><h2 class="text-3xl md:text-4xl font-bold text-gray-900">Simple, transparent pricing</h2><p class="text-gray-500 mt-2">No surprises – upgrade only when you need more</p></div>
            <div class="grid md:grid-cols-2 gap-8 max-w-4xl mx-auto">
                <div class="bg-white rounded-2xl shadow-lg overflow-hidden border border-gray-200 p-8">
                    <h3 class="text-2xl font-bold">Free</h3><p class="text-gray-500 mt-1">For personal projects</p>
                    <div class="mt-4"><span class="text-4xl font-bold">₦0</span> <span class="text-gray-500">/ month</span></div>
                    <ul class="mt-6 space-y-3">
                        <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> Monitor up to 3 websites</li>
                        <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> Email alerts</li>
                        <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> 5‑minute checks</li>
                        <li class="flex items-center text-gray-400"><i class="fas fa-times-circle w-5 mr-2"></i> Unlimited websites</li>
                    </ul>
                    <div class="mt-8"><a href="/signup" class="block text-center bg-indigo-600 text-white py-2 rounded-xl hover:bg-indigo-700 transition">Get started</a></div>
                </div>
                <div class="bg-white rounded-2xl shadow-xl overflow-hidden border-2 border-indigo-500 transform scale-105 md:scale-105">
                    <div class="bg-indigo-500 text-white text-center py-2 text-sm font-semibold">MOST POPULAR</div>
                    <div class="p-8">
                        <h3 class="text-2xl font-bold">Pro</h3><p class="text-gray-500 mt-1">For businesses & advanced users</p>
                        <div class="mt-4"><span class="text-4xl font-bold">₦15,000</span> <span class="text-gray-500">/ month</span></div>
                        <ul class="mt-6 space-y-3">
                            <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> Unlimited websites</li>
                            <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> Priority email alerts</li>
                            <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> 5‑minute checks</li>
                            <li class="flex items-center"><i class="fas fa-check-circle text-green-500 w-5 mr-2"></i> Response time history</li>
                        </ul>
                        <div class="mt-8"><a href="/signup" class="block text-center bg-indigo-600 text-white py-2 rounded-xl hover:bg-indigo-700 transition">Start free → Upgrade anytime</a></div>
                    </div>
                </div>
            </div>
        </div>
    </section>
    <section class="gradient-bg py-20 text-white">
        <div class="max-w-4xl mx-auto text-center px-4">
            <h2 class="text-3xl md:text-4xl font-bold">Ready to never miss downtime again?</h2><p class="text-indigo-100 text-lg mt-4">Join hundreds of users who trust Uptime Monitor.</p>
            <div class="mt-8"><a href="/signup" class="bg-white text-indigo-600 px-8 py-3 rounded-xl text-lg font-semibold shadow-lg hover:bg-gray-100 transition">Create free account →</a></div>
        </div>
    </section>
    <footer class="bg-gray-900 text-gray-400 py-12"><div class="max-w-7xl mx-auto px-4 text-center"><p>© 2026 Uptime Monitor. All rights reserved.</p><p class="mt-2 text-sm">Made with 🚀 by LAGOAH</p></div></footer>
</body>
</html>
    '''

# ---------- AUTHENTICATION ROUTES ----------
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
        <form method="post">
            <input name="email" placeholder="Email" required><br>
            <input name="password" type="password" placeholder="Password" required><br>
            <button type="submit">Sign Up</button>
        </form>
        <a href="/login">Already have an account? Login</a>
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
        <form method="post">
            <input name="email" placeholder="Email" required><br>
            <input name="password" type="password" placeholder="Password" required><br>
            <button type="submit">Login</button>
        </form>
        <a href="/signup">Sign up</a>
    '''

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

# ---------- DASHBOARD & WEBSITE MANAGEMENT ----------
@app.route("/dashboard")
@login_required
def dashboard():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    rows = ""
    for w in websites:
        status, rt = check_website(w.url)
        rows += f"<tr><td>{w.name}</td><td class='{status.lower()}'>{status}</td><td>{rt if rt else 'N/A'}</td><td><a href='/delete-website/{w.id}'>Delete</a></td></tr>"
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div style="color: red; margin-bottom: 10px;">⚠️ {msg}</div>'
    pro_badge = '<span style="background: gold; padding: 2px 8px; border-radius: 12px;">⭐ Pro</span>' if current_user.is_pro else '<a href="/upgrade" style="background: #28a745; color: white; padding: 2px 8px; text-decoration: none; border-radius: 12px;">Upgrade to Pro</a>'
    return f'''
        <h1>Welcome {current_user.email} {pro_badge}</h1>
        {flash_messages}
        <a href="/add-website">Add Website</a> | <a href="/logout">Logout</a>
        <table>
            <tr><th>Name</th><th>Status</th><th>Response (s)</th><th>Action</th></tr>
            {rows}
        </table>
        <br>
        <button onclick="location.reload()">Refresh Status</button>
    '''

@app.route("/add-website", methods=["GET", "POST"])
@login_required
def add_website():
    if request.method == "POST":
        name = request.form["name"]
        url = request.form["url"]
        if not current_user.is_pro and Website.query.filter_by(user_id=current_user.id).count() >= 3:
            flash("Free tier: max 3 websites. Upgrade to Pro for unlimited.")
            return redirect(url_for("dashboard"))
        website = Website(name=name, url=url, user_id=current_user.id)
        db.session.add(website)
        db.session.commit()
        return redirect(url_for("dashboard"))
    return '''
        <form method="post">
            <input name="name" placeholder="Site name (e.g., My Blog)" required><br>
            <input name="url" placeholder="https://..." required><br>
            <button type="submit">Add Website</button>
        </form>
        <a href="/dashboard">Cancel</a>
    '''

@app.route("/delete-website/<int:website_id>")
@login_required
def delete_website(website_id):
    website = Website.query.get_or_404(website_id)
    if website.user_id != current_user.id:
        flash("Unauthorized")
        return redirect(url_for("dashboard"))
    db.session.delete(website)
    db.session.commit()
    return redirect(url_for("dashboard"))

# ---------- PAYMENT & UPGRADE ----------
@app.route("/upgrade")
@login_required
def upgrade():
    if not PAYSTACK_SECRET_KEY:
        return "Paystack secret key missing", 500
    amount_kobo = 1500000
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
        else:
            flash("Payment initialization failed")
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
    r = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers)
    result = r.json()
    if result.get("status") and result["data"]["status"] == "success":
        email = result["data"]["customer"]["email"]
        user = User.query.filter_by(email=email).first()
        if user:
            user.is_pro = True
            db.session.commit()
            login_user(user, force=True)
            flash("Payment successful! Your account is now Pro.")
        else:
            flash("User not found")
    else:
        flash("Verification failed")
    return redirect(url_for("dashboard"))

# ---------- BACKGROUND CHECK ----------
@app.route("/update-all")
def update_all():
    for website in Website.query.all():
        user = User.query.get(website.user_id)
        if not user:
            continue
        current_status, _ = check_website(website.url)
        alert = Alert.query.filter_by(website_id=website.id).first()
        if not alert:
            alert = Alert(website_id=website.id, last_status=current_status)
            db.session.add(alert)
            db.session.commit()
            continue
        if alert.last_status != current_status:
            send_alert_email(user.email, website.name, current_status, website.url)
            alert.last_status = current_status
            alert.last_alert_sent = datetime.now()
            db.session.commit()
    return "Background check completed"

# ---------- CREATE TABLES ----------
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
