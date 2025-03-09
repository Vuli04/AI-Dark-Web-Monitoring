import aiohttp
import asyncio
import socks
import socket
import sqlite3
import os
import smtplib
import ssl
import requests
import threading
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from bs4 import BeautifulSoup
from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
from aiohttp_socks import ProxyConnector
from dotenv import load_dotenv
from fpdf import FPDF
import pyotp
from sklearn.feature_extraction.text import TfidfVectorizer

# Load environment variables
load_dotenv()

# ======= LOGGING SETUP =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("darkweb_monitoring.log"), logging.StreamHandler()],
)

# ======= CONFIGURE TOR PROXY =========
socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 9050)
socket.socket = socks.socksocket

# ======= DATABASE SETUP =========
conn = sqlite3.connect("darkweb_monitoring.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS darkweb_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        content TEXT,
        ai_analysis TEXT,
        sentiment TEXT,
        keywords TEXT,
        entities TEXT,
        acknowledged INTEGER DEFAULT 0,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

# ======= AI MODEL FOR CONTENT ANALYSIS =========
classifier = pipeline(
    "text-classification",
    model="distilbert-base-uncased-finetuned-sst-2-english",
)
tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased")

# ======= FLASK APP SETUP =========
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
socketio = SocketIO(app)

# ======= RATE LIMITING =========
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# ======= FLASK-LOGIN SETUP =========
login_manager = LoginManager()
login_manager.init_app(app)

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

# ======= TWO-FACTOR AUTHENTICATION =========
totp = pyotp.TOTP(os.getenv("TOTP_SECRET"))

# ======= ROUTES =========
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username == os.getenv("DASHBOARD_USER") and password == os.getenv("DASHBOARD_PASSWORD"):
            user = User(1)
            login_user(user)
            return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/trigger_scan", methods=["POST"])
@login_required
def trigger_scan():
    asyncio.create_task(run_darkweb_monitoring())
    return "Scan triggered successfully!", 200

@app.route("/acknowledge_alert/<int:alert_id>", methods=["POST"])
@login_required
def acknowledge_alert(alert_id):
    cursor.execute("UPDATE darkweb_data SET acknowledged=1 WHERE id=?", (alert_id,))
    conn.commit()
    return "Alert acknowledged!", 200

@app.route("/override_analysis/<int:record_id>", methods=["POST"])
@login_required
def override_analysis(record_id):
    new_label = request.form["new_label"]
    cursor.execute("UPDATE darkweb_data SET ai_analysis=? WHERE id=?", (new_label, record_id))
    conn.commit()
    return "Analysis overridden!", 200

@app.route("/generate_report")
@login_required
def generate_report():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt="Dark Web Monitoring Report", ln=True, align="C")
    
    cursor.execute("SELECT * FROM darkweb_data")
    for row in cursor.fetchall():
        pdf.cell(200, 10, txt=f"URL: {row[1]}", ln=True)
        pdf.cell(200, 10, txt=f"AI Analysis: {row[3]}", ln=True)
    
    pdf.output("report.pdf")
    return "Report generated successfully!", 200

# ======= TOR CIRCUIT REFRESH =========
async def refresh_tor_circuit():
    with requests.Session() as session:
        session.proxies = {
            "http": "socks5h://127.0.0.1:9050",
            "https": "socks5h://127.0.0.1:9050",
        }
        session.post("http://127.0.0.1:9051/control/newnym")

# ======= SCRAPE AHMIA FOR NEW .ONION SITES =========
def scrape_ahmia():
    ahmia_url = "https://ahmia.fi/search/?q=market"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        response = requests.get(ahmia_url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        onion_sites = set()
        for link in soup.find_all("a", href=True):
            if ".onion" in link["href"]:
                onion_sites.add(link["href"])
        
        return list(onion_sites)
    except Exception as e:
        logging.error(f"Error scraping Ahmia: {e}")
        return []

# ======= STORE NEW LINKS IN DATABASE =========
def store_links(onion_sites):
    for url in onion_sites:
        try:
            cursor.execute("INSERT INTO darkweb_data (url) VALUES (?)", (url,))
        except sqlite3.IntegrityError:
            continue  # Skip duplicates
    conn.commit()

# ======= ASYNC SCRAPER =========
async def fetch_site(session, url):
    try:
        async with session.get(url, timeout=15) as response:
            html = await response.text()
            logging.info(f"[+] Fetched: {url}")
            return html
    except Exception as e:
        logging.error(f"[-] Failed: {url} | Error: {e}")
        return None

async def scrape_dark_web():
    connector = ProxyConnector.from_url("socks5h://127.0.0.1:9050")
    async with aiohttp.ClientSession(connector=connector) as session:
        cursor.execute("SELECT url FROM darkweb_data")
        onion_sites = [row[0] for row in cursor.fetchall()]
        tasks = [fetch_site(session, url) for url in onion_sites]
        results = await asyncio.gather(*tasks)
        return dict(zip(onion_sites, results))

# ======= AI ANALYSIS FUNCTION =========
def analyze_dark_web_content(html_content):
    return classifier(html_content[:512])  # AI analyzes the first 512 characters

# ======= SAVE TO DATABASE FUNCTION =========
def save_to_db(url, content, ai_result):
    sentiment = "positive" if ai_result[0]["label"] == "POSITIVE" else "negative"
    keywords = extract_keywords(content)
    entities = extract_entities(content)
    cursor.execute("UPDATE darkweb_data SET content=?, ai_analysis=?, sentiment=?, keywords=?, entities=?, timestamp=CURRENT_TIMESTAMP WHERE url=?",
                   (content, str(ai_result), sentiment, str(keywords), str(entities), url))
    conn.commit()

# ======= EXTRACT KEYWORDS =========
def extract_keywords(content):
    vectorizer = TfidfVectorizer(stop_words="english", max_features=10)
    tfidf_matrix = vectorizer.fit_transform([content])
    feature_names = vectorizer.get_feature_names_out()
    return feature_names.tolist()

# ======= EXTRACT ENTITIES =========
def extract_entities(content):
    ner_pipeline = pipeline("ner", grouped_entities=True)
    entities = ner_pipeline(content[:512])  # Limit to first 512 characters
    return entities

# ======= MAIN MONITORING FUNCTION =========
async def run_darkweb_monitoring():
    logging.info("🚀 Starting Dark Web Monitoring...")
    scraped_data = await scrape_dark_web()
    
    for url, html in scraped_data.items():
        if html:
            ai_result = analyze_dark_web_content(html)
            save_to_db(url, html, str(ai_result))

            if "negative" in ai_result[0]["label"]:
                send_email_alert(url, str(ai_result))
                send_telegram_alert(url, str(ai_result))

            logging.info(f"✅ Data Saved for {url}")

    socketio.emit("update_dashboard", fetch_latest_data())

# ======= AUTOMATION =========
async def run_automation():
    while True:
        logging.info("[AI] Scraping Ahmia for new dark web sites...")
        new_sites = scrape_ahmia()
        if new_sites:
            store_links(new_sites)
            logging.info(f"[AI] Added {len(new_sites)} new .onion sites.")
        
        logging.info("[AI] Running AI-powered monitoring...")
        await run_darkweb_monitoring()
        
        logging.info("[AI] Sleeping for 24 hours before next scan...\n")
        await asyncio.sleep(86400)

# ======= MAIN FUNCTION =========
if __name__ == "__main__":
    # Start automation in a separate thread
    threading.Thread(target=lambda: asyncio.run(run_automation()), daemon=True).start()
    
    # Start Flask app
    socketio.run(app, debug=True)