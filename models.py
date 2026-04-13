from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    year = db.Column(db.String(20), nullable=False)
    has_voted = db.Column(db.Boolean, default=False)

class VoterRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    method = db.Column(db.String(20), nullable=False)
    identifier = db.Column(db.String(120), nullable=False, unique=True)
    year = db.Column(db.String(20), nullable=False)
    has_voted = db.Column(db.Boolean, default=False)

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    candidate = db.Column(db.String(120), nullable=False)
