from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    year = db.Column(db.String(20), nullable=False)
    has_voted = db.Column(db.Boolean, default=False)

class VoterRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    method = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(120), nullable=False)
    year = db.Column(db.String(20), nullable=False)
    has_voted = db.Column(db.Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("method", "identifier", "year", name="uq_voter_record_scope"),
    )

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    candidate = db.Column(db.String(120), nullable=False)

class EligibleVoter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(80), nullable=False)
    full_name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    student_id = db.Column(db.String(20), nullable=True)
