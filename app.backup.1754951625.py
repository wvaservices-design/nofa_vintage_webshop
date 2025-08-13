import os, sqlite3, smtplib
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")
    conn.commit()

    # migratie: zet legacy image_filename om naar product_images als die nog niet bestaat
    legacy = conn.execute("""
      SELECT p.id, p.image_filename
      FROM products p
      LEFT JOIN product_images pi ON pi.product_id = p.id
      WHERE pi.id IS NULL AND p.image_filename IS NOT NULL AND p.image_filename != ''
    """).fetchall()
    for row in legacy:
        conn.execute("INSERT INTO product_images (product_id, filename, sort_order, created_at) VALUES (?,?,?,?)",
                     (row["id"], row["image_filename"], 0, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def send_email(subject, body):
    smtp_server = os.getenv("SMTP_SERVER"); smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USERNAME"); smtp_pass = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("FROM_EMAIL");   admin_email = os.getenv("ADMIN_EMAIL")
    if not all([smtp_server, smtp_port, smtp_user, smtp_pass, from_email, admin_email]):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject; msg["From"] = from_email; msg["To"] = admin_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_server, int(smtp_port)) as server:
            server.starttls(); server.login(smtp_user, smtp_pass); server.send_message(msg)
        return True
    except Exception as e:
        print("Email error:", e); return False

def cover_images_map(conn, product_ids):
    if not product_ids: return {}
    q = """
      SELECT product_id, filename FROM product_images
      WHERE id IN (SELECT MIN(id) FROM product_images WHERE product_id IN (%s) GROUP BY product_id)
    """ % (",".join(["?"]*len(product_ids)))
    rows = conn.execute(q, product_ids).fetchall()
    return {r["product_id"]: r["filename"] for r in rows}

def all_images(conn, pid):
    return conn.execute("SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order, id", (pid,)).fetchall()

@app.route("/")
def index():
    conn = get_db()
    products = conn.execute("SELECT * FROM products WHERE is_sold=0 ORDER BY created_at DESC").fetchall()
    pids = [str(p["id"]) for p in products]
    highest = {}
    if pids:
        q = f"SELECT product_id, MAX(amount) as max_amount FROM bids WHERE product_id IN ({','.join(['?']*len(pids))}) GROUP BY product_id"
        rows = conn.execute(q, pids).fetchall()
        highest = {row["product_id"]: row["max_amount"] for row in rows}
    covers = cover_images_map(conn, pids)
    conn.close()
    return render_template("index.html", products=products, highest=highest, covers=covers)

@app.route("/product/<int:pid>")
def product_detail(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product: abort(404)
    bids = conn.execute("SELECT * FROM bids WHERE product_id=? ORDER BY amount DESC, created_at ASC", (pid,)).fetchall()
    highest = bids[0]["amount"] if bids else None
    images = all_images(conn, pid)
    conn.close()
    return render_template("product.html", product=product, bids=bids, highest=highest, images=images)

@app.route("/product/<int:pid>/bid", methods=["POST"])
def place_bid(pid):
    name = request.form.get("name","").strip()
    email = request.form.get("email","").strip()
    amount = request.form.get("amount","").strip()
    if not name or not email or not amount:
        flash("Vul alle velden in.", "error"); return redirect(url_for("product_detail", pid=pid))
    try: amount = float(amount.replace(",", "."))
    except ValueError:
        flash("Bedrag is ongeldig.", "error"); return redirect(url_for("product_detail", pid=pid))
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product: conn.close(); abort(404)
    highest_row = conn.execute("SELECT MAX(amount) as max_amount FROM bids WHERE product_id=?", (pid,)).fetchone()
    current_highest = highest_row["max_amount"] if highest_row and highest_row["max_amount"] is not None else product["price_start"]
    if amount <= current_highest:
        flash(f"Je bod moet hoger zijn dan €{current_highest:.2f}.", "error"); conn.close(); return redirect(url_for("product_detail", pid=pid))
    conn.execute("INSERT INTO bids (product_id, name, email, amount, created_at) VALUES (?,?,?,?,?)",
                 (pid, name, email, amount, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    send_email(subject=f"Nieuw bod op {product['title']}",
               body=f"Er is een nieuw bod van €{amount:.2f} door {name} ({email}) op product #{pid} - {product['title']}")
    flash("Je bod is geplaatst! We nemen contact op als je wint.", "success")
    return redirect(url_for("product_detail", pid=pid))

@app.route("/admin", methods=["GET","POST"])
def admin():
    admin_password = os.getenv("ADMIN_PASSWORD","")
    if request.method == "POST" and request.form.get("action") == "login":
        if request.form.get("password") != admin_password:
            flash("Onjuist wachtwoord.", "error")
        else:
            session["is_admin"] = True; flash("Ingelogd als admin.", "success"); return redirect(url_for("admin"))

    if not session.get("is_admin"): return render_template("admin_login.html")

    if request.method == "POST" and request.form.get("action") == "add_product":
        title = request.form.get("title","").strip()
        description = request.form.get("description","").strip()
        price_start = request.form.get("price_start","0").strip()
        images = request.files.getlist("images")
        if not title or not price_start or not images or (len(images)==1 and images[0].filename==""):
            flash("Titel, startprijs en minimaal 1 afbeelding zijn verplicht.", "error"); return redirect(url_for("admin"))
        try: price_start = float(price_start.replace(",", "."))
        except ValueError: flash("Startprijs ongeldig.", "error"); return redirect(url_for("admin"))

        saved_files = []
        for file in images:
            if not file or file.filename=="":
                continue
            if not allowed_file(file.filename):
                flash(f"Bestandstype niet toegestaan: {file.filename}", "error"); return redirect(url_for("admin"))
            filename = secure_filename(file.filename)
            base, ext = os.path.splitext(filename)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            i=1
            while os.path.exists(save_path):
                filename = f"{base}_{i}{ext}"
                save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                i+=1
            file.save(save_path)
            saved_files.append(filename)

        if not saved_files:
            flash("Upload minimaal één geldige afbeelding.", "error"); return redirect(url_for("admin"))

        conn = get_db()
        # gebruik eerste als cover (legacy veld blijft voor compatibiliteit)
        conn.execute("INSERT INTO products (title, description, price_start, image_filename, created_at) VALUES (?,?,?,?,?)",
                     (title, description, float(price_start), saved_files[0], datetime.utcnow().isoformat()))
        pid = conn.execute("SELECT last_insert_rowid() as lid").fetchone()["lid"]
        for idx, fn in enumerate(saved_files):
            conn.execute("INSERT INTO product_images (product_id, filename, sort_order, created_at) VALUES (?,?,?,?)",
                         (pid, fn, idx, datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        flash("Product met afbeeldingen toegevoegd.", "success")
        return redirect(url_for("admin"))

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
    if not session.get("is_admin"): abort(403)
    conn = get_db(); conn.execute("UPDATE products SET is_sold=1 WHERE id=?", (pid,)); conn.commit(); conn.close()
    flash("Product gemarkeerd als verkocht.", "success")
    return redirect(url_for("admin"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
