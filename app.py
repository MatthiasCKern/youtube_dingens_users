from flask import Flask, render_template, request, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os, zipfile, json
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key")

# Robust handling of the database URI
db_uri = os.environ.get("DATABASE_URL")
if not db_uri:
    raise RuntimeError("❌ SQLALCHEMY_DATABASE_URI (DATABASE_URL) is not set in environment.")
app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024  # 5 GB
ALLOWED_EXTENSIONS = {'zip', 'json'}

db = SQLAlchemy(app)

# Models
class User(db.Model):
    __tablename__ = "users"  # Explicitly named to avoid keyword conflicts
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.Text, nullable=False)
    events = db.relationship('WatchEvent', backref='user', lazy=True, cascade="all, delete-orphan")

class WatchEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False)
    channel = db.Column(db.String, nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

# Helpers
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_watch_events(json_data):
    events, entries = [], []
    for entry in json_data:
        if "time" in entry and "subtitles" in entry:
            try:
                timestamp = datetime.strptime(entry["time"], "%Y-%m-%dT%H:%M:%S.%fZ")
                channel = entry["subtitles"][0]["name"]
                entries.append((timestamp, channel))
            except Exception:
                continue
    entries.sort(key=lambda x: x[0])
    for i in range(len(entries)):
        current_time, channel = entries[i]
        if i < len(entries) - 1:
            next_time = entries[i + 1][0]
            delta = (next_time - current_time).seconds // 60
            duration = delta if delta < 30 else 0
        else:
            duration = 0
        events.append({"timestamp": current_time, "channel": channel, "duration": duration})
    return events

# Routes
@app.route("/init-db")
def init_db():
    with app.app_context():
        db.create_all()
    return "✅ Database initialized!"

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if User.query.filter_by(username=username).first():
            return "Benutzer existiert bereits."
        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            return redirect(url_for("dashboard"))
        return "Login fehlgeschlagen."
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard", methods=["GET"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    has_data = WatchEvent.query.filter_by(user_id=user_id).first() is not None

    if has_data:
        return redirect(url_for("results"))
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        file = request.files.get("file")
        if not file or file.filename == '' or not allowed_file(file.filename):
            return redirect(url_for("dashboard"))

        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[1].lower()
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(filepath)

        history_path = None
        if ext == 'zip':
            extract_path = os.path.join(app.config['UPLOAD_FOLDER'], "extracted")
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(filepath, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            for root, dirs, files in os.walk(extract_path):
                for f in files:
                    if "Wiedergabeverlauf" in f and f.endswith(".json"):
                        history_path = os.path.join(root, f)
                        break
        elif ext == 'json':
            history_path = filepath

        if not history_path or not os.path.exists(history_path):
            return "Wiedergabeverlauf.json nicht gefunden."

        with open(history_path, encoding="utf-8") as f:
            json_data = json.load(f)

        extracted = extract_watch_events(json_data)
        user_id = session["user_id"]

        WatchEvent.query.filter_by(user_id=user_id).delete()
        db.session.commit()

        for e in extracted:
            db.session.add(WatchEvent(
                timestamp=e["timestamp"],
                channel=e["channel"],
                duration=e["duration"],
                user_id=user_id
            ))
        db.session.commit()

        return redirect(url_for("results"))

    except Exception as e:
        import traceback
        print("❌ Upload error:", str(e))
        traceback.print_exc()
        return "Ein Fehler ist aufgetreten beim Verarbeiten der Datei."

@app.route("/results")
def results():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    user_events = WatchEvent.query.filter_by(user_id=user_id).all()

    channel_totals = defaultdict(int)
    for e in user_events:
        channel_totals[e.channel] += e.duration
    sorted_channels = sorted(channel_totals.items(), key=lambda x: -x[1])
    top_100_channels = [name for name, _ in sorted_channels[:100]]

    serialized = [{
        "timestamp": e.timestamp.isoformat(),
        "channel": e.channel,
        "duration": e.duration
    } for e in user_events]

    return render_template("result.html", watchEvents=serialized, top_channels=top_100_channels)

# Run
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
