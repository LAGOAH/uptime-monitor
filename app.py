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
    # Placeholder – you can add Resend or any email later
    print(f"Would send email to {user_email}: {website_name} is {status}")

# Routes
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return '''
        <h1>Uptime Monitor</h1>
        <a href="/login">Login</a> | <a href="/signup">Sign Up</a>
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

@app.route("/dashboard")
@login_required
def dashboard():
    websites = Website.query.filter_by(user_id=current_user.id).all()
    rows = ""
    for w in websites:
        status, rt = check_website(w.url)
        rows += f"</td><td>{w.name}</td><td>{status}</td><td>{rt if rt else 'N/A'}</td><td><a href='/delete-website/{w.id}'>Delete</a></td></tr>"
    
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div style="color: red;">⚠️ {msg}</div>'
    
    # Show Upgrade button if not pro, else Pro badge
    if current_user.is_pro:
        pro_badge = '<span style="background: gold; padding: 2px 8px; border-radius: 12px;">⭐ Pro</span>'
    else:
        pro_badge = '<a href="/upgrade" style="background: #28a745; color: white; padding: 2px 8px; text-decoration: none; border-radius: 12px;">Upgrade to Pro</a>'
    
    return f'''
        <h1>Welcome {current_user.email} {pro_badge}</h1>
        {flash_messages}
        <a href="/add-website">Add Website</a> | <a href="/logout">Logout</a>
        <table border="1" cellpadding="10">
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

@app.route("/upgrade")
@login_required
def upgrade():
    if not PAYSTACK_SECRET_KEY:
        return "Paystack secret key missing", 500
    amount_kobo = 1500000  # ₦15,000
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

# Create tables
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
