import os
import json
import random
import smtplib
from flask import Flask, render_template, request, redirect, url_for, session, flash
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps

# --- Database and App Setup ---
from models import db, Student, Vote # Import from models.py

app = Flask(__name__)

# --- Configuration ---
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devkey")
# Use SQLite for the database
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

# Voting time window
VOTING_START = datetime(2025, 10, 1, 7, 0, 0)
VOTING_END = datetime(2025, 10, 1, 19, 0, 0)

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
            flash("You must be logged in to view this page.")
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
            flash("You must use your CSU student email (@student.csuniv.edu).")
            return redirect(url_for("verify_email"))

        # Check if student exists or create a new one
        student = Student.query.filter_by(email=email).first()
        if not student:
            student = Student(email=email, year=session["year"])
            db.session.add(student)
            db.session.commit()

        # Check if the student has already voted
        if student.has_voted:
            flash("This email address has already been used to vote.")
            return redirect(url_for("verify_email"))

        otp = str(random.randint(100000, 999999))
        session["otp"] = otp
        session["email"] = email

        try:
            send_otp_email(email, otp)
            return redirect(url_for("otp"))
        except Exception as e:
            flash("Error sending email. Please try again.")
            print("Email error:", e) # For debugging
            return redirect(url_for("verify_email"))

    return render_template("verify_email.html")

@app.route("/otp", methods=["GET", "POST"])
def otp():
    # This route is kept simple and does not need database access.
    # The previous error was caused by adding incorrect DB logic here.
    if request.method == "POST":
        entered = request.form["otp"].strip()
        if entered == session.get("otp"):
            return redirect(url_for("vote"))
        else:
            flash("Invalid OTP. Try again.")
            return redirect(url_for("otp"))
    return render_template("otp.html")

@app.route("/vote", methods=["GET", "POST"])
def vote():
    now = datetime.now()
    if not (VOTING_START <= now <= VOTING_END):
        return render_template("message.html", title="Voting Closed", message="Voting is not currently open.")

    email = session.get("email")
    year = session.get("year")
    if not email or not year:
        return redirect(url_for("index"))

    # Find the student in the database
    student = Student.query.filter_by(email=email).first()
    if not student or student.has_voted:
        return render_template("message.html", title="Already Voted", message="Your vote has already been recorded.")

    candidates = load_candidates()
    year_candidates = candidates.get(year, [])

    if request.method == "POST":
        selected_candidates = request.form.getlist("candidates")
        if len(selected_candidates) > 10:
            flash("You can only select up to 10 candidates.")
            return redirect(url_for("vote"))

        # Create new Vote records for each selected candidate
        for candidate_name in selected_candidates:
            new_vote = Vote(student_id=student.id, candidate=candidate_name)
            db.session.add(new_vote)

        # Mark the student as having voted
        student.has_voted = True
        db.session.add(student)

        # Commit all changes to the database
        db.session.commit()

        # Clear session data after successful vote
        session.pop("email", None)
        session.pop("year", None)
        session.pop("otp", None)

        return render_template("success.html")

    return render_template("vote.html", candidates=year_candidates)

# --- Admin Routes (Unchanged) ---
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username == ADMIN_USER and password == ADMIN_PASS:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Invalid credentials.")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("You have been logged out.")
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
        flash("Candidate name cannot be empty.")
        return redirect(url_for("admin_dashboard"))
    
    candidates = load_candidates()
    if name not in candidates[year]:
        candidates[year].append(name)
        save_candidates(candidates)
        flash(f"Added '{name}' to {year}.")
    else:
        flash(f"'{name}' is already a candidate for {year}.")
    
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
        flash(f"Removed '{name}' from {year}.")
    else:
        flash(f"'{name}' was not found for {year}.")

    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    with app.app_context():
        db.create_all()  # Create database tables from models.py
    app.run(host="0.0.0.0", port=5000, debug=True)