from flask import Flask, render_template_string, jsonify
import requests
from datetime import datetime
import os

app = Flask(__name__)

websites = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "GitHub", "url": "https://www.github.com"},
]

status_cache = {
    "Google": {"status": "Not checked yet", "last_check": "Never", "response_time": "N/A"},
    "GitHub": {"status": "Not checked yet", "last_check": "Never", "response_time": "N/A"},
}

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
        button { padding: 10px 20px; font-size: 16px; cursor: pointer; }
    </style>
</head>
<body>
    <h1>📡 Uptime Monitor</h1>
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
