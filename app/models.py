from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import declared_attr

db = SQLAlchemy()


def now():
    return datetime.now(timezone.utc)


class Record(db.Model):
    __abstract__ = True
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime, default=now, nullable=False)


class User(UserMixin, Record):
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    admin = db.Column(db.Boolean, default=False, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)

    @property
    def is_active(self):
        return self.enabled


class Owned(Record):
    __abstract__ = True
    owner_id = db.Column(db.ForeignKey('user.id'), nullable=False, index=True)
    @declared_attr
    def owner(cls):
        return db.relationship(User)


class Document(Owned):
    title = db.Column(db.String(160), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(80), default='')
    digest = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(30), nullable=False)
    error = db.Column(db.String(255), default='')
    content = db.relationship('DocumentText', uselist=False, cascade='all, delete-orphan')
    __table_args__ = (db.UniqueConstraint('owner_id', 'digest'),)


class DocumentText(Record):
    document_id = db.Column(db.ForeignKey('document.id'), unique=True, nullable=False)
    text = db.Column(db.Text, nullable=False)


class Source(Record):
    title = db.Column(db.String(160), nullable=False)
    author = db.Column(db.String(120), default='')
    category = db.Column(db.String(80), default='')
    kind = db.Column(db.String(80), default='')
    url = db.Column(db.String(500), default='')
    description = db.Column(db.Text, default='')
    published = db.Column(db.String(10), default='')
    text = db.Column(db.Text, nullable=False)


class BatchJob(Owned):
    title = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(30), default='Pending')
    items = db.relationship('BatchJobItem', cascade='all, delete-orphan', order_by='BatchJobItem.id')


class BatchJobItem(Record):
    batch_id = db.Column(db.ForeignKey('batch_job.id'), nullable=False)
    document_id = db.Column(db.ForeignKey('document.id', ondelete='SET NULL'))
    document = db.relationship(Document)
    title = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(30), default='Pending')
    error = db.Column(db.String(255), default='')


class SimilarityAnalysis(Owned):
    document_id = db.Column(db.ForeignKey('document.id', ondelete='SET NULL'))
    batch_item_id = db.Column(db.ForeignKey('batch_job_item.id'), unique=True)
    title = db.Column(db.String(160), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    document_text = db.Column(db.Text, nullable=False)
    score = db.Column(db.Float, nullable=False)
    seconds = db.Column(db.Float, nullable=False)
    methods = db.Column(db.JSON, nullable=False)
    results = db.relationship('SimilarityResult', cascade='all, delete-orphan')
    report = db.relationship('Report', uselist=False, cascade='all, delete-orphan', back_populates='analysis')


class SimilarityResult(Record):
    analysis_id = db.Column(db.ForeignKey('similarity_analysis.id'), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    score = db.Column(db.Float, nullable=False)
    metadata_snapshot = db.Column(db.JSON, nullable=False)
    segments = db.relationship('MatchSegment', cascade='all, delete-orphan')


class MatchSegment(Record):
    result_id = db.Column(db.ForeignKey('similarity_result.id'), nullable=False)
    start = db.Column(db.Integer, nullable=False)
    end = db.Column(db.Integer, nullable=False)
    excerpt = db.Column(db.Text, nullable=False)
    score = db.Column(db.Float, nullable=False)


class Report(Owned):
    analysis_id = db.Column(db.ForeignKey('similarity_analysis.id'), unique=True, nullable=False)
    analysis = db.relationship(SimilarityAnalysis, back_populates='report')


class Notification(Owned):
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False)
    read = db.Column(db.Boolean, default=False)


class Feedback(Owned):
    title = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default='Pending')
    priority = db.Column(db.String(20), default='Normal')
    response = db.Column(db.Text, default='')


class AuditLog(Record):
    user_id = db.Column(db.Integer)
    username = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), default='')
    related = db.Column(db.String(80), default='')
    status = db.Column(db.String(30), default='Success')
    ip = db.Column(db.String(80), default='')


class ModelConfiguration(Record):
    threshold = db.Column(db.Float, default=.72)
    ngram_size = db.Column(db.Integer, default=3)
    methods = db.Column(db.JSON, nullable=False)


class ModelVersion(Record):
    snapshot = db.Column(db.JSON, nullable=False)
    username = db.Column(db.String(80), nullable=False)


class SystemSetting(Record):
    max_upload_mb = db.Column(db.Integer, default=5)
    allow_registration = db.Column(db.Boolean, default=True)
