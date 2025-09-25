import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from config import *
from models import db, Student, Vote
import random, string

# Flask setup
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///votes.db"
app.config["MAIL_SERVER"] = MAIL_SERVER
app.config["MAIL_PORT"] = MAIL_PORT
app.config["MAIL_USE_TLS"] = MAIL_USE_TLS
app.config["MAIL_USERNAME"] = MAIL_USERNAME
app.config["MAIL_PASSWORD"] = MAIL_PASSWORD

db.init_app(app)
mail = Mail(app)

# Load candidates from JSON
def load_candidates():
    with open("candidates.json", "r") as f:
        return json.load(f)

# Routes
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        year = request.form.get("year")
        session["year"] = year
        return redirect(url_for("verify_email"))
    return render_template("index.html")

@app.route("/verify", methods=["GET", "POST"])
def verify_email():
    if request.method == "POST":
        email = request.form.get("email").lower().strip()
        if not email.endswith(ALLOWED_DOMAIN):
            flash("You must use a CSU student email.")
            return redirect(url_for("verify_email"))

        # generate OTP
        otp = "".join(random.choices(string.digits, k=6))
        session["otp"] = otp
        session["email"] = email

        msg = Message("CSU Voting Verification", sender=MAIL_USERNAME, recipients=[email])
        msg.body = f"Your OTP code is {otp}"
        mail.send(msg)

        return redirect(url_for("otp"))
    return render_template("verify_email.html")

@app.route("/otp", methods=["GET", "POST"])
def otp():
    if request.method == "POST":
        code = request.form.get("otp")
        if code == session.get("otp"):
            # Save or get student
            student = Student.query.filter_by(email=session["email"]).first()
            if not student:
                student = Student(email=session["email"], year=session["year"])
                db.session.add(student)
                db.session.commit()

            if student.has_voted:
                flash("You have already voted.")
                return redirect(url_for("index"))

            return redirect(url_for("vote"))
        else:
            flash("Invalid OTP. Try again.")
    return render_template("otp.html")

@app.route("/vote", methods=["GET", "POST"])
def vote():
    now = datetime.now()
    if not (VOTING_START <= now <= VOTING_END):
        return "Voting is not open right now."

    candidates = load_candidates()
    year = session.get("year")
    ballot = candidates.get(year, [])

    if request.method == "POST":
        selected = request.form.getlist("candidates")
        if len(selected) > 10:
            flash("You can only select up to 10 candidates.")
            return redirect(url_for("vote"))

        student = Student.query.filter_by(email=session["email"]).first()
        for c in selected:
            vote = Vote(student_id=student.id, candidate=c)
            db.session.add(vote)
        student.has_voted = True
        db.session.commit()

        return render_template("success.html")

    return render_template("vote.html", candidates=ballot)

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
