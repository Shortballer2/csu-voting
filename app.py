import os
import json
import random
import smtplib
import ssl
import re
from flask import Flask, render_template, request, redirect, url_for, session, flash
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

# --- Database and App Setup ---
from models import db, Student, Vote, VoterRecord
from sqlalchemy import func

load_dotenv("csu-voting.env", override=True)

app = Flask(__name__)

# --- Configuration ---
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devkey")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///votes.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize database with the app
db.init_app(app)

# --- Create database tables ---
with app.app_context():
    db.create_all()

# --- Environment Variables & Constants ---
VOTING_PASSWORD = os.getenv("VOTING_PASSWORD")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp-mail.outlook.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password")
STUDENT_EMAIL_PATTERN = re.compile(r"^[a-z]+[a-z][a-z]+@student\.csuniv\.edu$")
STUDENT_ID_PATTERN = re.compile(r"^\d{7,10}$")

# --- Helper Functions ---
def load_candidates():
    if not os.path.exists("candidates.json"):
        default_candidates = {
            "Freshman": [],
            "Sophomore": [],
            "Junior": [],
            "Senior": [],
        }
        save_candidates(default_candidates)
        return default_candidates
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
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("Missing EMAIL_USER or EMAIL_PASS for SMTP authentication.")
    ssl_context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=ssl_context)
        server.ehlo()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)

# --- Decorators ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "is_authenticated" not in session:
            return redirect(url_for("public_login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin_logged_in" not in session:
            flash("You must be logged in to view this page.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function

# --- Public Routes ---
@app.route("/login", methods=["GET", "POST"])
def public_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == VOTING_PASSWORD:
            session["is_authenticated"] = True
            return redirect(url_for("index"))
        else:
            flash("Incorrect password. Please try again.", "danger")
    return render_template("login.html")

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        session["year"] = request.form["year"]
        return redirect(url_for("verify_email"))
    return render_template("index.html")

@app.route("/verify_email", methods=["GET", "POST"])
@login_required
def verify_email():
    if "year" not in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        verification_method = request.form.get("verification_method", "email")
        if verification_method == "student_id":
            student_id_number = request.form.get("student_id_number", "").strip()
            if not STUDENT_ID_PATTERN.match(student_id_number):
                flash("Enter a valid student ID number (7 to 10 digits).", "danger")
                return redirect(url_for("verify_email"))
            voter_record = VoterRecord.query.filter_by(
                method="student_id",
                identifier=student_id_number,
            ).first()
            if voter_record and voter_record.has_voted:
                flash("This student ID number has already been used to vote.", "warning")
                return redirect(url_for("verify_email"))
            if not voter_record:
                voter_record = VoterRecord(
                    method="student_id",
                    identifier=student_id_number,
                    year=session["year"],
                )
                db.session.add(voter_record)
                db.session.commit()
            session["voter_record_id"] = voter_record.id
            session.pop("otp", None)
            session.pop("email", None)
            return redirect(url_for("vote"))
        email = request.form.get("email", "").strip().lower()
        if not STUDENT_EMAIL_PATTERN.match(email):
            flash(
                "Use your CSU student email in this format: firstnamemiddleinitiallastname@student.csuniv.edu.",
                "danger",
            )
            return redirect(url_for("verify_email"))
        student = Student.query.filter_by(email=email).first()
        if not student:
            student = Student(email=email, year=session["year"])
            db.session.add(student)
            db.session.commit()
        voter_record = VoterRecord.query.filter_by(method="email", identifier=email).first()
        if voter_record and voter_record.has_voted:
            flash("This email address has already been used to vote.", "warning")
            return redirect(url_for("verify_email"))
        if not voter_record:
            voter_record = VoterRecord(method="email", identifier=email, year=session["year"])
            db.session.add(voter_record)
            db.session.commit()
        otp = str(random.randint(100000, 999999))
        session["otp"] = otp
        session["email"] = email
        session["voter_record_id"] = voter_record.id
        try:
            send_otp_email(email, otp)
            return redirect(url_for("otp"))
        except Exception as e:
            flash("Error sending email. Please try again.", "danger")
            print("Email error:", e)
            return redirect(url_for("verify_email"))
    return render_template("verify_email.html")

@app.route("/otp", methods=["GET", "POST"])
@login_required
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
@login_required
def vote():
    voter_record_id = session.get("voter_record_id")
    year = session.get("year")
    if not voter_record_id or not year:
        return redirect(url_for("index"))
    voter_record = VoterRecord.query.filter_by(id=voter_record_id).first()
    if not voter_record or voter_record.has_voted:
        return render_template("message.html", title="Already Voted", message="Your vote has already been recorded.")
    candidates = load_candidates()
    year_candidates = candidates.get(year, [])
    if request.method == "POST":
        selected_candidates = request.form.getlist("candidates")
        write_in = request.form.get("write_in_candidate", "").strip()
        if write_in:
            selected_candidates.append(write_in)
        if len(selected_candidates) > 10:
            flash("You can only select up to 10 candidates (including write-ins).", "warning")
            return redirect(url_for("vote"))
        if not selected_candidates:
            flash("You must select at least one candidate to vote.", "warning")
            return redirect(url_for("vote"))
        for candidate_name in selected_candidates:
            new_vote = Vote(candidate=candidate_name)
            db.session.add(new_vote)
        voter_record.has_voted = True
        db.session.add(voter_record)
        db.session.commit()
        session.pop("email", None)
        session.pop("year", None)
        session.pop("otp", None)
        session.pop("voter_record_id", None)
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

# UPDATED: Route for handling manual vote submission from admin panel
@app.route("/admin/manual_vote", methods=["POST"])
@admin_login_required
def manual_vote():
    email = (request.form.get("email") or "").strip().lower()
    student_id_number = (request.form.get("student_id_number") or "").strip()
    year = request.form.get("year")
    
    if not year:
        flash("Class Year is required.", "danger")
        return redirect(url_for("admin_dashboard"))
    if not email and not student_id_number:
        flash("Either student email or student ID number is required.", "danger")
        return redirect(url_for("admin_dashboard"))

    # Get checked candidates from the form
    selected_candidates = request.form.getlist("candidates")
    # Get the write-in value from the form
    write_in = request.form.get("write_in_name", "").strip()
    if write_in:
        selected_candidates.append(write_in)

    # Validate number of votes
    if len(selected_candidates) > 10:
        flash("You can only select up to 10 candidates (including write-ins).", "warning")
        return redirect(url_for("admin_dashboard"))
    if not selected_candidates:
        flash("You must select at least one candidate to vote.", "warning")
        return redirect(url_for("admin_dashboard"))

    identifier = student_id_number or email
    method = "student_id" if student_id_number else "email"
    if method == "student_id" and not STUDENT_ID_PATTERN.match(student_id_number):
        flash("Student ID number must be 7 to 10 digits.", "danger")
        return redirect(url_for("admin_dashboard"))
    if method == "email":
        if not STUDENT_EMAIL_PATTERN.match(email):
            flash("Enter a valid CSU student email.", "danger")
            return redirect(url_for("admin_dashboard"))
        student = Student.query.filter_by(email=email).first()
        if not student:
            student = Student(email=email, year=year)
            db.session.add(student)

    voter_record = VoterRecord.query.filter_by(method=method, identifier=identifier).first()
    if voter_record and voter_record.has_voted:
        flash(f"Voter '{identifier}' has already voted.", "warning")
        return redirect(url_for("admin_dashboard"))
    if not voter_record:
        voter_record = VoterRecord(method=method, identifier=identifier, year=year)
        db.session.add(voter_record)
        db.session.flush()

    # Record the votes
    for candidate_name in selected_candidates:
        new_vote = Vote(candidate=candidate_name)
        db.session.add(new_vote)

    voter_record.has_voted = True
    db.session.add(voter_record)
    
    db.session.commit()

    flash(f"Successfully cast {len(selected_candidates)} vote(s) on behalf of '{identifier}'.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/results")
@admin_login_required
def results():
    vote_counts = db.session.query(
        Vote.candidate, 
        func.count(Vote.candidate).label('total_votes')
    ).group_by(Vote.candidate).order_by(func.count(Vote.candidate).desc()).all()
    return render_template("results.html", results=vote_counts)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
