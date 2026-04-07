from flask_sqlalchemy import SQLAlchemy
from datetime import datetime


db = SQLAlchemy()


class Building(db.Model):
    __tablename__ = "building"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    total_items = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Building {self.id} - {self.name}>"


class RepairLog(db.Model):
    __tablename__ = "repair_log"
    id = db.Column(db.Integer, primary_key=True)
    building = db.Column(db.String(64), nullable=False)
    date = db.Column(db.Date, nullable=False)
    item_name = db.Column(db.String(128), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    zone = db.Column(db.String(64), nullable=False)
    fault_desc = db.Column(db.Text, nullable=False)
    status = db.Column(db.Enum('fixable', 'unfixable'), nullable=False)
    notes = db.Column(db.Text)
    job_status = db.Column(db.String(16), nullable=False, default='open')
    final_result = db.Column(db.String(16), nullable=True)
    closed_date = db.Column(db.Date, nullable=True)
    close_note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<RepairLog {self.id} - {self.item_name}>"


class RepairEvent(db.Model):
    __tablename__ = "repair_event"
    id = db.Column(db.Integer, primary_key=True)
    repair_log_id = db.Column(db.Integer, db.ForeignKey('repair_log.id'), nullable=False, index=True)
    event_type = db.Column(db.String(32), nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(128), nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    repair_log = db.relationship('RepairLog', backref=db.backref('events', lazy=True, order_by='RepairEvent.event_date.asc()'))

    def __repr__(self):
        return f"<RepairEvent {self.id} - {self.event_type}>"
