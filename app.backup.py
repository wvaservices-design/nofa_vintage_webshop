import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort
from werkzeug.utils import secure_filename
from datetime import datetime
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "store.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-key")
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR

os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        price_start REAL NOT NULL,
        image_filename TEXT,
        is_sold INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bids (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        amount REAL NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")
    conn.commit()
    conn.close()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def send_email(subject, body):
    # Only send if SMTP settings are present
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("FROM_EMAIL")
    admin_email = os.getenv("ADMIN_EMAIL")

    if not all([smtp_server, smtp_port, smtp_user, smtp_pass, from_email, admin_email]):
        return False  # silently skip in demo

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = admin_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp_server, int(smtp_port)) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True
    except Exception as e:
        print("Email error:", e)
        return False

@app.route("/")
def index():
    conn = get_db()
    products = conn.execute("SELECT * FROM products WHERE is_sold=0 ORDER BY created_at DESC").fetchall()
    # Get highest bids for products in one query
    product_ids = [str(p["id"]) for p in products]
    highest = {}
    if product_ids:
        q = f"SELECT product_id, MAX(amount) as max_amount FROM bids WHERE product_id IN ({','.join(['?']*len(product_ids))}) GROUP BY product_id"
        rows = conn.execute(q, product_ids).fetchall()
        highest = {row["product_id"]: row["max_amount"] for row in rows}
    conn.close()
    return render_template("index.html", products=products, highest=highest)

@app.route("/product/<int:pid>")
def product_detail(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        abort(404)
    bids = conn.execute("SELECT * FROM bids WHERE product_id=? ORDER BY amount DESC, created_at ASC", (pid,)).fetchall()
    highest = bids[0]["amount"] if bids else None
    conn.close()
    return render_template("product.html", product=product, bids=bids, highest=highest)

@app.route("/product/<int:pid>/bid", methods=["POST"])
def place_bid(pid):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    amount = request.form.get("amount", "").strip()

    if not name or not email or not amount:
        flash("Vul alle velden in.", "error")
        return redirect(url_for("product_detail", pid=pid))

    try:
        amount = float(amount.replace(",", "."))
    except ValueError:
        flash("Bedrag is ongeldig.", "error")
        return redirect(url_for("product_detail", pid=pid))

    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        conn.close()
        abort(404)

    # Check higher than start price and current highest
    highest_row = conn.execute("SELECT MAX(amount) as max_amount FROM bids WHERE product_id=?", (pid,)).fetchone()
    current_highest = highest_row["max_amount"] if highest_row and highest_row["max_amount"] is not None else product["price_start"]
    if amount <= current_highest:
        flash(f"Je bod moet hoger zijn dan €{current_highest:.2f}.", "error")
        conn.close()
        return redirect(url_for("product_detail", pid=pid))

    conn.execute(
        "INSERT INTO bids (product_id, name, email, amount, created_at) VALUES (?,?,?,?,?)",
        (pid, name, email, amount, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    send_email(
        subject=f"Nieuw bod op {product['title']}",
        body=f"Er is een nieuw bod van €{amount:.2f} door {name} ({email}) op product #{pid} - {product['title']}"
    )

    flash("Je bod is geplaatst! We nemen contact op als je wint.", "success")
    return redirect(url_for("product_detail", pid=pid))

@app.route("/admin", methods=["GET", "POST"])
def admin():
    # very basic auth: password in form compared to env var
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if request.method == "POST" and request.form.get("action") == "login":
        if request.form.get("password") != admin_password:
            flash("Onjuist wachtwoord.", "error")
        else:
            # set a simple session flag
            from flask import session
            session["is_admin"] = True
            flash("Ingelogd als admin.", "success")
            return redirect(url_for("admin"))

    from flask import session
    if not session.get("is_admin"):
        return render_template("admin_login.html")

    if request.method == "POST" and request.form.get("action") == "add_product":
        title = request.form.get("title","").strip()
        description = request.form.get("description","").strip()
        price_start = request.form.get("price_start","0").strip()
        image = request.files.get("image")

        if not title or not price_start or not image:
            flash("Titel, startprijs en afbeelding zijn verplicht.", "error")
            return redirect(url_for("admin"))

        try:
            price_start = float(price_start.replace(",", "."))
        except ValueError:
            flash("Startprijs ongeldig.", "error")
            return redirect(url_for("admin"))

        if not allowed_file(image.filename):
            flash("Bestandstype niet toegestaan.", "error")
            return redirect(url_for("admin"))

        filename = secure_filename(image.filename)
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        # Avoid overwrite
        base, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(save_path):
            filename = f"{base}_{i}{ext}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            i += 1
        image.save(save_path)

        conn = get_db()
        conn.execute(
            "INSERT INTO products (title, description, price_start, image_filename, created_at) VALUES (?,?,?,?,?)",
            (title, description, price_start, filename, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
        flash("Product toegevoegd.", "success")
        return redirect(url_for("admin"))

    # list products and bids
    conn = get_db()
    products = conn.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()
    bids_by_product = {}
    for p in products:
        bids = conn.execute("SELECT * FROM bids WHERE product_id=? ORDER BY amount DESC", (p["id"],)).fetchall()
        bids_by_product[p["id"]] = bids
    conn.close()

    return render_template("admin.html", products=products, bids_by_product=bids_by_product)

@app.route("/admin/mark_sold/<int:pid>", methods=["POST"])
def mark_sold(pid):
    from flask import session
    if not session.get("is_admin"):
        abort(403)
    conn = get_db()
    conn.execute("UPDATE products SET is_sold=1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash("Product gemarkeerd als verkocht.", "success")
    return redirect(url_for("admin"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # vul je ADMIN_PASSWORD en (optioneel) SMTP in
python app.py
wva@air-van-wouter nofa_vintage_webshop



