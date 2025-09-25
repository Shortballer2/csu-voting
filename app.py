import os
import json
import random
import smtplib
from flask import Flask, render_template, request, redirect, url_for, session, flash
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps

# --- Database and App Setup ---
from models import db, Student, Vote

app = Flask(__name__)

# --- Configuration ---
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devkey")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///votes.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize database
db.init_app(app)

# --- Environment Variables & Constants ---
# Email settings
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))

# Admin credentials
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password")

# --- Helper Functions ---
def load_candidates():
    with open("candidates.json") as f:
        return json.load(f)

def save_candidates(data):
    with open("candidates.json", "w") as f:
        json.dump(data, f, indent=2)

def send_otp_email(to_email, otp):
    msg = MIMEText(f"Your CSU Voting OTP code is: {otp}")
    msg["Subject"] = "CSU Voting OTP Code"
    msg["From"] = EMAIL_USER
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)

# --- Decorators ---
def admin_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin_logged_in" not in session:
            flash("You must be logged in to view this page.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function

# --- Public Routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        session["year"] = request.form["year"]
        return redirect(url_for("verify_email"))
    return render_template("index.html")

@app.route("/verify_email", methods=["GET", "POST"])
def verify_email():
    if "year" not in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form["email"].strip().lower()
        if not email.endswith("@student.csuniv.edu"):
            flash("You must use your CSU student email (@student.csuniv.edu).", "danger")
            return redirect(url_for("verify_email"))

        student = Student.query.filter_by(email=email).first()
        if not student:
            student = Student(email=email, year=session["year"])
            db.session.add(student)
            db.session.commit()

        if student.has_voted:
            flash("TESTING-CODE-RELOAD: You already voted.", "warning")
            return redirect(url_for("verify_email"))

        otp = str(random.randint(100000, 999999))
        session["otp"] = otp
        session["email"] = email

        try:
            send_otp_email(email, otp)
            return redirect(url_for("otp"))
        except Exception as e:
            flash("Error sending email. Please try again.", "danger")
            print("Email error:", e)
            return redirect(url_for("verify_email"))

    return render_template("verify_email.html")

@app.route("/otp", methods=["GET", "POST"])
def otp():
    if request.method == "POST":
        entered = request.form["otp"].strip()
        if entered == session.get("otp"):
            return redirect(url_for("vote"))
        else:
            flash("Invalid OTP. Try again.", "danger")
            return redirect(url_for("otp"))
    return render_template("otp.html")

@app.route("/vote", methods=["GET", "POST"])
def vote():
    # --- The date check below is now disabled ---
    # now = datetime.now()
    # if not (VOTING_START <= now <= VOTING_END):
    #     return render_template("message.html", title="Voting Closed", message="Voting is not currently open.")

    email = session.get("email")
    year = session.get("year")
    if not email or not year:
        return redirect(url_for("index"))

    student = Student.query.filter_by(email=email).first()
    if not student or student.has_voted:
        return render_template("message.html", title="Already Voted", message="Your vote has already been recorded.")

    candidates = load_candidates()
    year_candidates = candidates.get(year, [])

    if request.method == "POST":
        selected_candidates = request.form.getlist("candidates")
        if len(selected_candidates) > 10:
            flash("You can only select up to 10 candidates.", "warning")
            return redirect(url_for("vote"))

        for candidate_name in selected_candidates:
            new_vote = Vote(student_id=student.id, candidate=candidate_name)
            db.session.add(new_vote)

        student.has_voted = True
        db.session.add(student)
        db.session.commit()

        session.pop("email", None)
        session.pop("year", None)
        session.pop("otp", None)

        return render_template("success.html")

    return render_template("vote.html", candidates=year_candidates)

# --- Admin Routes ---
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username == ADMIN_USER and password == ADMIN_PASS:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Invalid credentials.", "danger")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_login_required
def admin_dashboard():
    candidates = load_candidates()
    return render_template("admin_dashboard.html", candidates=candidates)

@app.route("/admin/add", methods=["POST"])
@admin_login_required
def add_candidate():
    year = request.form["year"]
    name = request.form["name"].strip()
    if not name:
        flash("Candidate name cannot be empty.", "warning")
        return redirect(url_for("admin_dashboard"))

    candidates = load_candidates()
    if name not in candidates[year]:
        candidates[year].append(name)
        save_candidates(candidates)
        flash(f"Added '{name}' to {year}.", "success")
    else:
        flash(f"'{name}' is already a candidate for {year}.", "warning")
    
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/delete", methods=["POST"])
@admin_login_required
def delete_candidate():
    year = request.form["year"]
    name = request.form["name"]

    candidates = load_candidates()
    if name in candidates[year]:
        candidates[year].remove(name)
        save_candidates(candidates)
        flash(f"Removed '{name}' from {year}.", "success")
    else:
        flash(f"'{name}' was not found for {year}.", "danger")

    return redirect(url_for("admin_dashboard"))

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)