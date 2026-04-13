import os
import json
import random
import smtplib
import ssl
import re
import io
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

# --- Database and App Setup ---
from models import db, Student, Vote, VoterRecord, EligibleVoter
from sqlalchemy import func
from pypdf import PdfReader

load_dotenv("csu-voting.env", override=True)

app = Flask(__name__)

# --- Configuration ---
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devkey")
database_url = os.getenv("DATABASE_URL", "").strip()
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    default_db_path = Path(os.getenv("DB_PATH", "data/votes.db")).expanduser()
    default_db_path.parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{default_db_path}"
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
STUDENT_EMAIL_DOMAIN = "@student.csuniv.edu"

# --- Helper Functions ---
def load_candidates():
    if not os.path.exists("candidates.json"):
        default_ballots = {
            "General Election": {
                "description": "Select up to 10 options.",
                "questions": [
                    {
                        "prompt": "Question 1",
                        "max_selections": 10,
                        "options": [],
                    }
                ],
            }
        }
        save_candidates(default_ballots)
        return default_ballots
    with open("candidates.json") as f:
        data = json.load(f)
    normalized_data = {}
    if isinstance(data, dict):
        for ballot_name, ballot_data in data.items():
            if isinstance(ballot_data, list):
                normalized_data[ballot_name] = {
                    "description": "",
                    "questions": [
                        {
                            "prompt": "Question 1",
                            "max_selections": 10,
                            "options": ballot_data,
                        }
                    ],
                }
            elif isinstance(ballot_data, dict):
                questions = ballot_data.get("questions")
                if not isinstance(questions, list):
                    legacy_options = ballot_data.get("options", [])
                    questions = [
                        {
                            "prompt": "Question 1",
                            "max_selections": ballot_data.get("max_selections", 10),
                            "options": legacy_options,
                        }
                    ]
                normalized_questions = []
                for question in questions:
                    if not isinstance(question, dict):
                        continue
                    normalized_question = {
                        "prompt": (question.get("prompt") or "Question").strip(),
                        "max_selections": validate_max_selections(question.get("max_selections"), default=1),
                        "options": [
                            str(option).strip()
                            for option in question.get("options", [])
                            if str(option).strip()
                        ],
                    }
                    show_if = parse_show_if_rule(question)
                    if show_if:
                        normalized_question["show_if"] = show_if
                    normalized_questions.append(normalized_question)
                if not normalized_questions:
                    normalized_questions = [
                        {
                            "prompt": "Question 1",
                            "max_selections": 10,
                            "options": [],
                        }
                    ]
                normalized_data[ballot_name] = {
                    "description": (ballot_data.get("description") or "").strip(),
                    "questions": normalized_questions,
                }
    if not normalized_data:
        normalized_data = {
            "General Election": {
                "description": "Select up to 10 options.",
                "questions": [
                    {
                        "prompt": "Question 1",
                        "max_selections": 10,
                        "options": [],
                    }
                ],
            }
        }
    if normalized_data != data:
        save_candidates(normalized_data)
    return normalized_data

def normalize_student_email(value):
    email_value = (value or "").strip().lower()
    if not email_value:
        return ""
    if "@" not in email_value:
        email_value = f"{email_value}{STUDENT_EMAIL_DOMAIN}"
    return email_value

def normalize_student_id(value):
    return re.sub(r"\D", "", (value or "").strip())

def normalize_name(value):
    return " ".join((value or "").strip().lower().split())

def parse_options(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]

def parse_eligible_voters_pdf(file_storage):
    reader = PdfReader(io.BytesIO(file_storage.read()))
    parsed_rows = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        for raw_line in page_text.splitlines():
            line = " ".join(raw_line.split())
            if not line:
                continue
            columns = [col.strip() for col in re.split(r"[,\t|]+", line) if col.strip()]
            if len(columns) < 3:
                continue
            email = next((col for col in columns if STUDENT_EMAIL_PATTERN.match(normalize_student_email(col))), "")
            student_id = next((normalize_student_id(col) for col in columns if STUDENT_ID_PATTERN.match(normalize_student_id(col))), "")
            if not email or not student_id:
                continue
            name_parts = [col for col in columns if col != email and normalize_student_id(col) != student_id]
            full_name = " ".join(name_parts).strip()
            if not full_name:
                continue
            parsed_rows.append(
                {
                    "full_name": full_name,
                    "email": normalize_student_email(email),
                    "student_id": student_id,
                }
            )
    return parsed_rows



def parse_show_if_rule(question):
    if not isinstance(question, dict):
        return None
    show_if = question.get("show_if")
    if not isinstance(show_if, dict):
        return None
    question_number = show_if.get("question_number")
    option = str(show_if.get("option") or "").strip()
    try:
        question_number = int(question_number)
    except (TypeError, ValueError):
        return None
    if question_number < 1 or not option:
        return None
    return {"question_number": question_number, "option": option}


def question_is_visible(question, answers_by_index):
    show_if = question.get("show_if")
    if not isinstance(show_if, dict):
        return True
    parent_index = int(show_if.get("question_number", 0)) - 1
    option = str(show_if.get("option") or "").strip()
    if parent_index < 0 or not option:
        return True
    return option in answers_by_index.get(parent_index, [])

def parse_questions_json(text):
    try:
        parsed = json.loads(text or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    normalized_questions = []
    for index, question in enumerate(parsed, start=1):
        if not isinstance(question, dict):
            continue
        prompt = (question.get("prompt") or f"Question {index}").strip()
        max_selections = validate_max_selections(question.get("max_selections"), default=1)
        options = [
            str(option).strip()
            for option in question.get("options", [])
            if str(option).strip()
        ]
        normalized_question = {
            "prompt": prompt,
            "max_selections": max_selections,
            "options": options,
        }
        show_if = parse_show_if_rule(question)
        if show_if:
            normalized_question["show_if"] = show_if
        normalized_questions.append(normalized_question)
    return normalized_questions

def save_candidates(data):
    with open("candidates.json", "w") as f:
        json.dump(data, f, indent=2)

def validate_max_selections(value, default=10):
    try:
        parsed_value = int(value)
        if parsed_value < 1:
            return default
        return parsed_value
    except (TypeError, ValueError):
        return default

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
    return redirect(url_for("verify_email"))

@app.route("/verify_email", methods=["GET", "POST"])
@login_required
def verify_email():
    elections = load_candidates()
    if request.method == "POST":
        selected_election = request.form.get("year", "").strip()
        if selected_election not in elections:
            flash("Please choose a valid ballot.", "warning")
            return redirect(url_for("verify_email"))
        session["year"] = selected_election
        full_name = (request.form.get("full_name") or "").strip()
        normalized_full_name = normalize_name(full_name)
        email = normalize_student_email(request.form.get("email"))
        student_id_number = normalize_student_id(request.form.get("student_id_number"))
        if len(normalized_full_name) < 3:
            flash("Enter your full name exactly as listed in the voter roster.", "danger")
            return redirect(url_for("verify_email"))
        if not STUDENT_EMAIL_PATTERN.match(email):
            flash(
                "Use your CSU student email in this format: firstnamemiddleinitiallastname@student.csuniv.edu.",
                "danger",
            )
            return redirect(url_for("verify_email"))
        if not STUDENT_ID_PATTERN.match(student_id_number):
            flash("Enter a valid student ID number (6 digits).", "danger")
            return redirect(url_for("verify_email"))
        roster_exists = (
            db.session.query(EligibleVoter.id)
            .filter_by(year=selected_election)
            .first()
            is not None
        )
        if roster_exists:
            eligible_voter = EligibleVoter.query.filter_by(
                year=selected_election,
                email=email,
                student_id=student_id_number,
            ).first()
            if not eligible_voter or normalize_name(eligible_voter.full_name) != normalized_full_name:
                flash("Your details could not be verified against the eligible voter list.", "danger")
                return redirect(url_for("verify_email"))

        verification_method = request.form.get("verification_method", "email")
        if verification_method == "student_id":
            voter_record = VoterRecord.query.filter_by(
                method="student_id",
                identifier=student_id_number,
                year=selected_election,
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
        student = Student.query.filter_by(email=email).first()
        if not student:
            student = Student(email=email, year=session["year"])
            db.session.add(student)
            db.session.commit()
        voter_record = VoterRecord.query.filter_by(
            method="email",
            identifier=email,
            year=selected_election,
        ).first()
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
    return render_template("verify_email.html", elections=list(elections.keys()))

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
    ballots = load_candidates()
    ballot = ballots.get(year, {"questions": [], "description": ""})
    questions = ballot.get("questions", [])
    if request.method == "POST":
        selected_candidates = []
        answers_by_index = {}
        for index, question in enumerate(questions):
            if not question_is_visible(question, answers_by_index):
                continue
            question_choices = request.form.getlist(f"question_{index}_candidates")
            write_in = request.form.get(f"question_{index}_write_in", "").strip()
            if write_in:
                question_choices.append(write_in)
            question_max = question.get("max_selections", 1)
            if len(question_choices) > question_max:
                flash(
                    f"'{question.get('prompt', f'Question {index + 1}')}' allows up to {question_max} selections.",
                    "warning",
                )
                return redirect(url_for("vote"))
            answers_by_index[index] = question_choices
            selected_candidates.extend(question_choices)
        if not selected_candidates:
            flash("You must answer at least one question option to vote.", "warning")
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
    return render_template(
        "vote.html",
        questions=questions,
        ballot_name=year,
        ballot_description=ballot.get("description") or "",
    )

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
    roster_counts = dict(
        db.session.query(EligibleVoter.year, func.count(EligibleVoter.id))
        .group_by(EligibleVoter.year)
        .all()
    )
    election_names = list(candidates.keys())
    return render_template(
        "admin_dashboard.html",
        candidates=candidates,
        voter_records=voter_records,
        election_names=election_names,
        roster_counts=roster_counts,
    )

@app.route("/admin/eligible_voters/upload", methods=["POST"])
@admin_login_required
def upload_eligible_voters():
    year = request.form.get("year", "").strip()
    pdf_file = request.files.get("eligible_voters_pdf")
    if not year:
        flash("Election / ballot is required.", "danger")
        return redirect(url_for("admin_dashboard"))
    if not pdf_file or not pdf_file.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF file.", "danger")
        return redirect(url_for("admin_dashboard"))
    try:
        parsed_rows = parse_eligible_voters_pdf(pdf_file)
    except Exception:
        flash("Could not read the PDF. Please upload a text-based PDF roster.", "danger")
        return redirect(url_for("admin_dashboard"))
    if not parsed_rows:
        flash("No valid voters were found. Expected rows with name, email, and 6-digit student ID.", "warning")
        return redirect(url_for("admin_dashboard"))

    EligibleVoter.query.filter_by(year=year).delete(synchronize_session=False)
    seen = set()
    inserted = 0
    for row in parsed_rows:
        key = (row["email"], row["student_id"])
        if key in seen:
            continue
        seen.add(key)
        db.session.add(
            EligibleVoter(
                year=year,
                full_name=row["full_name"],
                email=row["email"],
                student_id=row["student_id"],
            )
        )
        inserted += 1
    db.session.commit()
    flash(f"Uploaded {inserted} eligible voter records for '{year}'.", "success")
    return redirect(url_for("admin_dashboard"))

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
    candidates[election_name] = {
        "description": "",
        "questions": [
            {
                "prompt": "Question 1",
                "max_selections": 10,
                "options": [],
            }
        ],
    }
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
        candidates[year] = {
            "description": "",
            "questions": [{"prompt": "Question 1", "max_selections": 10, "options": []}],
        }
    questions = candidates[year].setdefault("questions", [])
    if not questions:
        questions = [{"prompt": "Question 1", "max_selections": 10, "options": []}]
        candidates[year]["questions"] = questions
    options = questions[0].setdefault("options", [])
    if name not in options:
        options.append(name)
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
    questions = candidates[year].setdefault("questions", [])
    if not questions:
        flash(f"'{year}' has no configured questions.", "danger")
        return redirect(url_for("admin_dashboard"))
    options = questions[0].setdefault("options", [])
    if name in options:
        options.remove(name)
        save_candidates(candidates)
        flash(f"Removed '{name}' from {year}.", "success")
    else:
        flash(f"'{name}' was not found for {year}.", "danger")
    return redirect(url_for("admin_dashboard"))

# UPDATED: Route for handling manual vote submission from admin panel
@app.route("/admin/manual_vote", methods=["POST"])
@admin_login_required
def manual_vote():
    email = normalize_student_email(request.form.get("email"))
    student_id_number = normalize_student_id(request.form.get("student_id_number"))
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
    ballot = candidates[year]
    selected_candidates = []
    answers_by_index = {}
    for index, question in enumerate(ballot.get("questions", [])):
        if not question_is_visible(question, answers_by_index):
            continue
        question_choices = request.form.getlist(f"question_{index}_candidates")
        write_in = request.form.get(f"question_{index}_write_in", "").strip()
        if write_in:
            question_choices.append(write_in)
        question_max = question.get("max_selections", 1)
        if len(question_choices) > question_max:
            flash(
                f"'{question.get('prompt', f'Question {index + 1}')}' allows up to {question_max} selections.",
                "warning",
            )
            return redirect(url_for("admin_dashboard"))
        answers_by_index[index] = question_choices
        selected_candidates.extend(question_choices)
    if not selected_candidates:
        flash("You must select at least one option to vote.", "warning")
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

    voter_record = VoterRecord.query.filter_by(method=method, identifier=identifier, year=year).first()
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

@app.route("/admin/ballot/update", methods=["POST"])
@admin_login_required
def update_ballot():
    ballot_name = request.form.get("ballot_name", "").strip()
    description = (request.form.get("description") or "").strip()
    candidates = load_candidates()
    if ballot_name not in candidates:
        flash("Selected ballot was not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    questions = []
    prompts = request.form.getlist("question_prompt[]")
    max_values = request.form.getlist("question_max_selections[]")
    option_blocks = request.form.getlist("question_options[]")
    show_if_questions = request.form.getlist("question_show_if_question[]")
    show_if_options = request.form.getlist("question_show_if_option[]")
    for index in range(max(len(prompts), len(max_values), len(option_blocks), len(show_if_questions), len(show_if_options))):
        prompt = (prompts[index] if index < len(prompts) else "").strip()
        options_text = option_blocks[index] if index < len(option_blocks) else ""
        options = parse_options(options_text)
        max_selections = validate_max_selections(
            max_values[index] if index < len(max_values) else None,
            default=1,
        )
        if not prompt and not options:
            continue
        if not prompt:
            prompt = f"Question {len(questions) + 1}"
        question_data = {
            "prompt": prompt,
            "max_selections": max_selections,
            "options": options,
        }
        show_if_question = (show_if_questions[index] if index < len(show_if_questions) else "").strip()
        show_if_option = (show_if_options[index] if index < len(show_if_options) else "").strip()
        if show_if_question or show_if_option:
            show_if = parse_show_if_rule(
                {"show_if": {"question_number": show_if_question, "option": show_if_option}}
            )
            if show_if is None:
                flash(f"Question {index + 1} has an invalid conditional branch rule.", "danger")
                return redirect(url_for("admin_dashboard"))
            question_data["show_if"] = show_if
        questions.append(question_data)

    if not questions:
        # Backward compatibility for previous JSON-only editor UI.
        questions_json = request.form.get("questions_json", "")
        if questions_json:
            parsed_json_questions = parse_questions_json(questions_json)
            if parsed_json_questions is None:
                flash("Questions must be valid JSON.", "danger")
                return redirect(url_for("admin_dashboard"))
            questions = parsed_json_questions

    if not questions:
        flash("At least one question is required for a ballot.", "danger")
        return redirect(url_for("admin_dashboard"))
    candidates[ballot_name]["description"] = description
    candidates[ballot_name]["questions"] = questions
    save_candidates(candidates)
    flash(f"Updated ballot builder settings for '{ballot_name}'.", "success")
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
