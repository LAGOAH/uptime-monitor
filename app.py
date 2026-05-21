# app.py - Uptime Monitor with Resend Email Alerts

from flask import Flask, render_template_string, jsonify
import requests
import time
import threading
from datetime import datetime
import resend

app = Flask(__name__)

# ========== CONFIGURATION ==========
# List of websites to monitor
websites = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "GitHub", "url": "https://www.github.com"},
]

# Resend API key (paste yours below)
resend.api_key = "re_gYLBeA6y_2X7TpevfDutuYXh1P6A5NgTr"  # <-- REPLACE THIS with your actual Resend API key

# Where to send alerts
ALERT_EMAIL = "lazarusgodswillahmadu@gmail.com"
# ===================================

status_cache = {}
previous_status = {site["name"]: "UNKNOWN" for site in websites}

def check_website(url):
    try:
        start_time = time.time()
        response = requests.get(url, timeout=5)
        end_time = time.time()
        response_time = round(end_time - start_time, 3)
        if 200 <= response.status_code < 400:
            return "UP", response_time
        else:
            return "DOWN", response_time
    except:
        return "DOWN", None

def send_alert(website_name, status, response_time):
    """Send email alert using Resend API."""
    params = {
        "from": "Uptime Monitor <onboarding@resend.dev>",
        "to": [ALERT_EMAIL],
        "subject": f"⚠️ Alert: {website_name} is {status}",
        "html": f"""
        <strong>Website:</strong> {website_name}<br>
        <strong>Status:</strong> {status}<br>
        <strong>Response Time:</strong> {response_time}<br>
        <strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """,
    }
    try:
        resend.Emails.send(params)
        print(f"📧 Alert sent for {website_name} ({status})")
    except Exception as e:
        print(f"Failed to send email: {e}")

def update_status():
    while True:
        for site in websites:
            name = site["name"]
            url = site["url"]
            status, response_time = check_website(url)

            status_cache[name] = {
                "status": status,
                "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "response_time": response_time if response_time else "N/A"
            }

            if status != previous_status[name]:
                send_alert(name, status, response_time)
                previous_status[name] = status

            print(f"[{status_cache[name]['last_check']}] {name} is {status}")
        time.sleep(60)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Uptime Monitor</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        .up { color: green; font-weight: bold; }
        .down { color: red; font-weight: bold; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #f2f2f2; }
    </style>
</head>
<body>
    <h1>📡 Uptime Monitor Dashboard</h1>
    <p>Last updated every 60 seconds. Alerts sent when status changes.</p>
    <table>
        <tr><th>Website</th><th>Status</th><th>Last Check</th><th>Response Time</th></tr>
        {% for site in websites %}
        <tr>
            <td>{{ site.name }}</td>
            <td class="{% if status_cache[site.name].status == 'UP' %}up{% else %}down{% endif %}">
                {{ status_cache[site.name].status }}
            </td>
            <td>{{ status_cache[site.name].last_check }}</td>
            <td>{{ status_cache[site.name].response_time }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE, websites=websites, status_cache=status_cache)

@app.route('/api/status')
def api_status():
    return jsonify(status_cache)

thread = threading.Thread(target=update_status, daemon=True)
thread.start()

if __name__ == '__main__':
    print("Uptime Monitor with Resend Alerts started!")
    print(f"Alerts will be sent to: {ALERT_EMAIL}")
    print("Open your browser to: http://127.0.0.1:5000")
    app.run(debug=True, use_reloader=False)
