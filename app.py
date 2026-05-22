from flask import Flask, render_template_string, redirect, url_for, request, flash, get_flashed_messages
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
from datetime import datetime

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

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return '''
        <h1>Uptime Monitor</h1>
        <p>Monitor your websites. Get alerts when they go down.</p>
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
        rows += f"<tr><td>{w.name}</td><td>{status}</td><td>{rt if rt else 'N/A'}</td><td><a href='/delete-website/{w.id}'>Delete</a></td></tr>"
    
    flash_messages = ""
    for msg in get_flashed_messages():
        flash_messages += f'<div style="color: red; margin-bottom: 10px;">⚠️ {msg}</div>'
    
    return f'''
        <h1>Welcome {current_user.email}</h1>
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

@app.route("/update-all")
def update_all():
    # Will be used for background checking (Day 3)
    return "Background check endpoint ready"

# Create tables (only once)
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
