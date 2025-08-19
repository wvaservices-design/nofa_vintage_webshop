import os, sqlite3, smtplib
from datetime import datetime
from email.message import EmailMessage
import cloudinary
import cloudinary.uploader
import zipfile, tempfile, shutil
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for
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

@app.context_processor
def _img_helpers():
    import os
    def img_src(fn):
        if not fn:
            return url_for('static', filename='placeholder.png')
        s = str(fn)
        if s.startswith('http://') or s.startswith('https://'):
            return s
        # lokaal uploadpad
        candidate = os.path.join(app.config["UPLOAD_FOLDER"], s)
        if os.path.exists(candidate):
            return url_for('uploaded_file', filename=s)
        # fallback
        return url_for('static', filename='placeholder.png')
    return dict(img_src=img_src)


# --- Cloudinary config ---
cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
api_key    = os.getenv("CLOUDINARY_API_KEY")
api_secret = os.getenv("CLOUDINARY_API_SECRET")
if cloud_name and api_key and api_secret:
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)
else:
    print("Cloudinary niet geconfigureerd (missing env vars). Uploads vallen terug op lokaal pad.")

def upload_to_cdn(file_storage, public_id_prefix="nofa"):
    try:
        if not (cloud_name and api_key and api_secret):
            return None  # geen Cloudinary, caller kan lokaal opslaan
        # file_storage: werkzeug FileStorage
        res = cloudinary.uploader.upload(
            file_storage,
            folder=public_id_prefix,
            resource_type="image",
            overwrite=False
        )
        return res.get("secure_url") or res.get("url")
    except Exception as e:
        print("Cloudinary upload fout:", e)
        return None

app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB
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

    # migratie legacy cover -> product_images
    legacy = conn.execute("""
      SELECT p.id, p.image_filename
      FROM products p
      LEFT JOIN product_images pi ON pi.product_id = p.id
      WHERE pi.id IS NULL AND p.image_filename IS NOT NULL AND p.image_filename != ''
    """).fetchall()
    for row in legacy:
        conn.execute("INSERT INTO product_images (product_id, filename, sort_order, created_at) VALUES (?,?,?,?)",
                     (row["id"], row["image_filename"], 0, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _email_config_snapshot():
    keys = ["SMTP_SERVER","SMTP_PORT","SMTP_USERNAME","FROM_EMAIL","ADMIN_EMAIL"]
    return {k: os.getenv(k) for k in keys}

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
    if not product_ids:
        return {}
    placeholders = ",".join(["?"] * len(product_ids))
    sql = f"""
      SELECT pi.product_id, pi.filename
      FROM product_images pi
      JOIN (
        SELECT product_id, MIN(sort_order) AS ms, MIN(id) AS mid
        FROM product_images
        WHERE product_id IN ({placeholders})
        GROUP BY product_id
      ) x ON x.product_id = pi.product_id AND pi.sort_order = x.ms
      GROUP BY pi.product_id
    """
    rows = conn.execute(sql, product_ids).fetchall()
    return {row["product_id"]: row["filename"] for row in rows}



def images_count_map(conn, product_ids):
    # open een eigen verbinding zodat we nooit op een gesloten conn werken
    if not product_ids:
        return {}
    c = get_db()
    try:
        placeholders = ",".join(["?"]*len(product_ids))
        sql = f"SELECT product_id, COUNT(*) AS cnt FROM product_images WHERE product_id IN ({placeholders}) GROUP BY product_id"
        rows = c.execute(sql, product_ids).fetchall()
        return {row["product_id"]: row["cnt"] for row in rows}
    finally:
        c.close()

def all_images(conn, pid):
    return conn.execute("SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order, id", (pid,)).fetchall()

def update_cover(conn, pid):
    row = conn.execute("SELECT filename FROM product_images WHERE product_id=? ORDER BY sort_order, id LIMIT 1", (pid,)).fetchone()
    cover = row["filename"] if row else None
    conn.execute("UPDATE products SET image_filename=? WHERE id=?", (cover, pid))
    conn.commit()

@app.route("/")
def index():
    conn = get_db()
    products = conn.execute("SELECT * FROM products ORDER BY is_sold ASC, created_at DESC").fetchall()
    pids = [str(p["id"]) for p in products]
    highest = {}
    if pids:
        q = f"SELECT product_id, MAX(amount) as max_amount FROM bids WHERE product_id IN ({','.join(['?']*len(pids))}) GROUP BY product_id"
        rows = conn.execute(q, pids).fetchall()
        highest = {row["product_id"]: row["max_amount"] for row in rows}
    covers = cover_images_map(conn, pids)
    conn.close()
    image_counts = images_count_map(conn, pids)
    return render_template("index.html", products=products, highest=highest, covers=covers, image_counts=image_counts)

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
    try:
        product = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not product:
            flash("Product niet gevonden.", "error")
            return redirect(url_for("index"))

        # huidige hoogste bod bepalen
        highest_row = conn.execute("SELECT MAX(amount) as max_amount FROM bids WHERE product_id=?", (pid,)).fetchone()
        current_highest = highest_row["max_amount"] if highest_row and highest_row["max_amount"] is not None else product["price_start"]

        if amount <= current_highest:
            flash(f"Je bod moet hoger zijn dan €{current_highest:.2f}.", "error")
            return redirect(url_for("product_detail", pid=pid))

        # bod opslaan
        conn.execute(
            "INSERT INTO bids (product_id, name, email, amount, created_at) VALUES (?,?,?,?,?)",
            (pid, name, email, amount, datetime.utcnow().isoformat())
        )
        conn.commit()

    finally:
        conn.close()

    # mail sturen (best effort)
    try:
        send_email(
            subject=f"Nieuw bod op {product['title']}",
            body=f"Er is een nieuw bod van €{amount:.2f} door {name} ({email}) op product #{pid} - {product['title']}"
        )
    except Exception:
        pass

    flash("Je bod is geplaatst! We nemen contact op als je wint.", "success")
    return redirect(url_for("product_detail", pid=pid))

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
    ok_mail = send_email(
        subject=f"Nieuw bod op {product['title']}",
        body=f"Er is een nieuw bod van €{amount:.2f} door {name} ({email}) op product #{pid} - {product['title']}"
    )
    print("[BID EMAIL]", {"product_id": pid, "amount": amount, "bidder": email, "mail_ok": ok_mail})
@app.route("/admin", methods=["GET", "POST"], endpoint="admin")
def admin():
    admin_password = os.getenv("ADMIN_PASSWORD", "")
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

@app.route("/admin/edit/<int:pid>", methods=["GET","POST"])
def admin_edit(pid):
    if not session.get("is_admin"):
        abort(403)

    conn = get_db()
    cur = conn.cursor()
    product = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        conn.close()
        flash("Product niet gevonden.", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        title = request.form.get("title","").strip()
        description = request.form.get("description","").strip()
        price_start = request.form.get("price_start","").strip()
        is_sold = 1 if request.form.get("is_sold") == "on" else 0

        if not title:
            flash("Titel is verplicht.", "error")
            conn.close()
            return redirect(url_for("admin_edit", pid=pid))

        try:
            price_start = float(price_start.replace(",", ".")) if price_start else product["price_start"]
        except ValueError:
            flash("Startprijs ongeldig.", "error")
            conn.close()
            return redirect(url_for("admin_edit", pid=pid))

        cur.execute(
            "UPDATE products SET title=?, description=?, price_start=?, is_sold=? WHERE id=?",
            (title or product["title"], description, price_start, is_sold, pid)
        )
        conn.commit()
        conn.close()
        flash("Product bijgewerkt.", "success")
        return redirect(url_for("admin_edit", pid=pid))

    conn.close()
    return render_template("admin_edit.html", product=product)

    from flask import session
    if not session.get("is_admin"):
        abort(403)

    conn = get_db()
    cur = conn.cursor()

    # product ophalen
    product = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not product:
        conn.close()
        flash("Product niet gevonden.", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        title = request.form.get("title","").strip()
        description = request.form.get("description","").strip()
        price_start = request.form.get("price_start","").strip()
        is_sold = 1 if request.form.get("is_sold") == "on" else 0

        if not title:
            flash("Titel is verplicht.", "error")
            conn.close()
            return redirect(url_for("admin_edit", pid=pid))

        try:
            price_start = float(price_start.replace(",", ".")) if price_start else product["price_start"]
        except ValueError:
            flash("Startprijs ongeldig.", "error")
            conn.close()
            return redirect(url_for("admin_edit", pid=pid))

        # velden bijwerken
        cur.execute(
            "UPDATE products SET title=?, description=?, price_start=?, is_sold=? WHERE id=?",
            (title or product["title"], description, price_start, is_sold, pid)
        )
        conn.commit()

        # Optioneel: extra foto's toevoegen als er een product_images tabel is
        try:
            cols = cur.execute("PRAGMA table_info(product_images)").fetchall()
            has_images = bool(cols)
        except Exception:
            has_images = False

        if has_images:
            files = request.files.getlist("images")
            saved = 0
            for f in files:
                if not f or not getattr(f, "filename", ""):
                    continue
                # Probeer Cloudinary, anders lokaal
                url = None
                try:
                    if 'upload_to_cdn' in globals():
                        url = upload_to_cdn(f, public_id_prefix="nofa")
                except Exception:
                    url = None
                if url:
                    filename = url
                else:
                    # lokaal opslaan
                    if not allowed_file(f.filename):
                        continue
                    from werkzeug.utils import secure_filename
                    import os
                    filename = secure_filename(f.filename)
                    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    base, ext = os.path.splitext(filename); i=1
                    while os.path.exists(save_path):
                        filename = f"{base}_{i}{ext}"; save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename); i+=1
                    f.save(save_path)
                # sort_order bepalen
                rowo = cur.execute("SELECT COALESCE(MAX(sort_order), -1) as maxo FROM product_images WHERE product_id=?", (pid,)).fetchone()
                next_order = (rowo["maxo"] + 1) if rowo else 0
                cur.execute(
                    "INSERT INTO product_images (product_id, filename, sort_order, created_at) VALUES (?,?,?,?)",
                    (pid, filename, next_order, datetime.utcnow().isoformat())
                )
                saved += 1
            if saved:
                conn.commit()

        conn.close()
        flash("Product bijgewerkt.", "success")
        return redirect(url_for("admin_edit", pid=pid))

    conn.close()
    return render_template("admin_edit.html", product=product)



@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/admin/mark_sold/<int:pid>", methods=["POST"])
def mark_sold(pid):
    if not session.get("is_admin"):
        abort(403)
    conn = get_db()
    conn.execute("UPDATE products SET is_sold=1 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash("Product gemarkeerd als verkocht.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/delete/<int:pid>", methods=["POST"], endpoint="admin_delete_product")
def admin_delete_product(pid):
    if not session.get("is_admin"):
        abort(403)

    conn = get_db()
    cur = conn.cursor()

    # Bestaan er extra afbeeldingen?
    has_images = False
    try:
        cols = cur.execute("PRAGMA table_info(product_images)").fetchall()
        has_images = bool(cols)
    except Exception:
        has_images = False

    # Verzamel alle bestandsnamen die lokaal kunnen staan
    to_delete = []

    # hoofdafbeelding van product
    row = cur.execute("SELECT image_filename FROM products WHERE id=?", (pid,)).fetchone()
    if row and row["image_filename"]:
        to_delete.append(row["image_filename"])

    # extra afbeeldingen
    if has_images:
        rows = cur.execute("SELECT filename FROM product_images WHERE product_id=?", (pid,)).fetchall()
        for r in rows:
            if r["filename"]:
                to_delete.append(r["filename"])

    # Verwijder biedingen
    cur.execute("DELETE FROM bids WHERE product_id=?", (pid,))

    # Verwijder extra afbeeldingen records
    if has_images:
        cur.execute("DELETE FROM product_images WHERE product_id=?", (pid,))

    # Verwijder product
    cur.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()

    # Probeer lokale bestanden te verwijderen (geen http/https)
    import os
    for fn in to_delete:
        s = str(fn)
        if s.startswith("http://") or s.startswith("https://"):
            continue
        try:
            path = os.path.join(app.config["UPLOAD_FOLDER"], s)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    flash("Product en bijbehorende data verwijderd.", "success")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)