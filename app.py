from flask import Flask, render_template_string, jsonify, redirect
import requests
from datetime import datetime
import os
from smartpaystack import SmartPaystack, ChargeStrategy, Currency

app = Flask(__name__)

websites = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "GitHub", "url": "https://www.github.com"},
]

status_cache = {
    "Google": {"status": "Not checked yet", "last_check": "Never", "response_time": "N/A"},
    "GitHub": {"status": "Not checked yet", "last_check": "Never", "response_time": "N/A"},
}

ALERT_EMAIL = "lazarusgodswillahmadu@gmail.com"

# Paystack configuration (uses environment variable on Render)
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
paystack_client = SmartPaystack(secret_key=PAYSTACK_SECRET_KEY)

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

@app.route('/update')
def update():
    for site in websites:
        name = site["name"]
        url = site["url"]
        status, response_time = check_website(url)
        status_cache[name] = {
            "status": status,
            "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "response_time": response_time if response_time else "N/A"
        }
    return jsonify({"message": "Updated", "status": status_cache})

@app.route('/create-checkout-session')
def create_checkout_session():
    amount_in_ngn = 15000
    customer_email = ALERT_EMAIL
    response = paystack_client.create_charge(
        email=customer_email,
        amount=amount_in_ngn,
        currency=Currency.NGN,
        charge_strategy=ChargeStrategy.PASS,
        metadata={"user_email": customer_email}
    )
    payment_url = response.get('authorization_url')
    if payment_url:
        return redirect(payment_url, code=303)
    else:
        return jsonify({"error": "Could not initialize payment"}), 500

@app.route('/payment-success')
def payment_success():
    return "<h1>Payment Successful! Thank you for upgrading to Pro!</h1>"

@app.route('/payment-cancel')
def payment_cancel():
    return "<h1>Payment was cancelled. You can try again anytime.</h1>"

@app.route('/')
def dashboard():
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Uptime Monitor</title>
    <style>
        body { font-family: Arial; margin: 40px; }
        .UP { color: green; font-weight: bold; }
        .DOWN { color: red; font-weight: bold; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #f2f2f2; }
        button, .btn { padding: 10px 20px; font-size: 16px; cursor: pointer; margin: 5px; }
        .btn-pro { background-color: #28a745; color: white; text-decoration: none; border-radius: 5px; display: inline-block; }
    </style>
</head>
<body>
    <h1>📡 Uptime Monitor</h1>
    <a href="/create-checkout-session" class="btn btn-pro">🚀 Upgrade to Pro (₦15,000/month)</a>
    <button onclick="fetch('/update').then(() => location.reload())">🔄 Check Now</button>
    <table>
        <tr><th>Website</th><th>Status</th><th>Last Check</th><th>Response Time</th></tr>
        {% for site in websites %}
        <tr>
            <td>{{ site.name }}</td>
            <td class="{{ status_cache[site.name].status }}">{{ status_cache[site.name].status }}</td>
            <td>{{ status_cache[site.name].last_check }}</td>
            <td>{{ status_cache[site.name].response_time }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
    """
    return render_template_string(html, websites=websites, status_cache=status_cache)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
