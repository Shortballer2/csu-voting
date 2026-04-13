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
STUDENT_ID_PATTERN = re.compile(r"^\d{6}$")

# --- Helper Functions ---
def load_candidates():
    if not os.path.exists("candidates.json"):
        default_candidates = {"General Election": []}
        save_candidates(default_candidates)
        return default_candidates
    with open("candidates.json") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not data:
        data = {"General Election": []}
        save_candidates(data)
    return data

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
    elections = load_candidates()
    if request.method == "POST":
        selected_election = request.form.get("year", "").strip()
        if selected_election not in elections:
            flash("Please choose a valid ballot.", "warning")
            return redirect(url_for("index"))
        session["year"] = selected_election
        return redirect(url_for("verify_email"))
    return render_template("index.html", elections=list(elections.keys()))

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
                flash("Enter a valid student ID number (6 digits).", "danger")
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
    voter_records = VoterRecord.query.order_by(VoterRecord.id.desc()).limit(100).all()
    election_names = list(candidates.keys())
    return render_template(
        "admin_dashboard.html",
        candidates=candidates,
        voter_records=voter_records,
        election_names=election_names,
    )

@app.route("/admin/election/add", methods=["POST"])
@admin_login_required
def add_election():
    election_name = request.form.get("election_name", "").strip()
    if not election_name:
        flash("Election/ballot name cannot be empty.", "warning")
        return redirect(url_for("admin_dashboard"))
    candidates = load_candidates()
    if election_name in candidates:
        flash(f"'{election_name}' already exists.", "warning")
        return redirect(url_for("admin_dashboard"))
    candidates[election_name] = []
    save_candidates(candidates)
    flash(f"Created election/ballot '{election_name}'.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/election/rename", methods=["POST"])
@admin_login_required
def rename_election():
    current_name = request.form.get("current_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not current_name or not new_name:
        flash("Both current and new election names are required.", "danger")
        return redirect(url_for("admin_dashboard"))
    candidates = load_candidates()
    if current_name not in candidates:
        flash(f"Election/ballot '{current_name}' was not found.", "danger")
        return redirect(url_for("admin_dashboard"))
    if new_name in candidates and new_name != current_name:
        flash(f"Election/ballot '{new_name}' already exists.", "warning")
        return redirect(url_for("admin_dashboard"))
    candidates[new_name] = candidates.pop(current_name)
    save_candidates(candidates)
    VoterRecord.query.filter_by(year=current_name).update({"year": new_name}, synchronize_session=False)
    Student.query.filter_by(year=current_name).update({"year": new_name}, synchronize_session=False)
    db.session.commit()
    flash(f"Renamed election/ballot '{current_name}' to '{new_name}'.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/election/delete", methods=["POST"])
@admin_login_required
def delete_election():
    election_name = request.form.get("election_name", "").strip()
    candidates = load_candidates()
    if election_name not in candidates:
        flash(f"Election/ballot '{election_name}' was not found.", "danger")
        return redirect(url_for("admin_dashboard"))
    if len(candidates) == 1:
        flash("You must keep at least one election/ballot configured.", "warning")
        return redirect(url_for("admin_dashboard"))
    candidates.pop(election_name)
    save_candidates(candidates)
    VoterRecord.query.filter_by(year=election_name).delete(synchronize_session=False)
    Student.query.filter_by(year=election_name).delete(synchronize_session=False)
    db.session.commit()
    flash(f"Deleted election/ballot '{election_name}' and its voter records.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/add", methods=["POST"])
@admin_login_required
def add_candidate():
    year = request.form.get("year", "").strip()
    name = request.form["name"].strip()
    if not year:
        flash("Election/ballot is required.", "warning")
        return redirect(url_for("admin_dashboard"))
    if not name:
        flash("Candidate name cannot be empty.", "warning")
        return redirect(url_for("admin_dashboard"))
    candidates = load_candidates()
    if year not in candidates:
        candidates[year] = []
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
    if year not in candidates:
        flash(f"Election/ballot '{year}' was not found.", "danger")
        return redirect(url_for("admin_dashboard"))
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
        flash("Election/ballot is required.", "danger")
        return redirect(url_for("admin_dashboard"))
    candidates = load_candidates()
    if year not in candidates:
        flash("Please choose a valid election/ballot.", "danger")
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
        flash("Student ID number must be 6 digits.", "danger")
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

@app.route("/admin/voter_records/update", methods=["POST"])
@admin_login_required
def update_voter_record():
    record_id = request.form.get("record_id", type=int)
    has_voted = request.form.get("has_voted") == "on"
    if not record_id:
        flash("A voter record ID is required.", "danger")
        return redirect(url_for("admin_dashboard"))

    voter_record = db.session.get(VoterRecord, record_id)
    if not voter_record:
        flash("Voter record not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    voter_record.has_voted = has_voted
    db.session.add(voter_record)
    db.session.commit()
    status_text = "has voted" if has_voted else "not voted"
    flash(f"Updated voter '{voter_record.identifier}' to {status_text}.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/voter_records/reset", methods=["POST"])
@admin_login_required
def reset_voter_records():
    year = request.form.get("year", "").strip()
    query = VoterRecord.query.filter_by(has_voted=True)
    if year:
        query = query.filter_by(year=year)
    updated_count = query.update({"has_voted": False}, synchronize_session=False)
    db.session.commit()
    if year:
        flash(f"Reset {updated_count} voter record(s) for '{year}'.", "success")
    else:
        flash(f"Reset {updated_count} voter record(s) across all years.", "success")
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
