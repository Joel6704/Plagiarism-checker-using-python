import csv
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import StringIO
from urllib.parse import urlsplit

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .batches import advance_batch
from .models import (
    AuditLog, BatchJob, BatchJobItem, Document, Feedback, ModelConfiguration,
    ModelVersion, Notification, Report, SimilarityAnalysis, SimilarityResult,
    Source, SystemSetting, User, db,
)
from .services import DISCLAIMER, METHODS, analyze, audit, compare, extract, notify, pdf_report, upload

pages = Blueprint('pages', __name__)
FEEDBACK = ('Bug Report', 'Feature Request', 'General Feedback', 'Report Issue')
STATES = ('Pending', 'Reviewed', 'Resolved')
PRIORITIES = ('Low', 'Normal', 'High')


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def scoped(model):
    return model.query if current_user.admin else model.query.filter_by(owner_id=current_user.id)


def owned(model, identifier):
    return scoped(model).filter_by(id=identifier).first_or_404()


def value(name, size=160):
    return request.form.get(name, '').strip()[:size]


def finish(endpoint, message=None, **kwargs):
    db.session.commit()
    if message:
        flash(message, 'success')
    return redirect(url_for(f'pages.{endpoint}', **kwargs))


def filtered(query, column, owner=None, status=None, category=None):
    term = request.args.get('q', '').strip()[:160]
    if term:
        columns = column if isinstance(column, tuple) else (column,)
        query = query.filter(or_(*(field.ilike(f'%{term}%') for field in columns)))
    for param, field in [('owner', owner), ('status', status), ('category', category)]:
        if field is not None and request.args.get(param):
            term = request.args[param][:80]
            query = query.filter(field.ilike(f'%{term}%') if param == 'owner' else field == term)
    if request.args.get('date'):
        try:
            day = date.fromisoformat(request.args['date'])
            query = query.filter(func.date(query.column_descriptions[0]['entity'].created) == day.isoformat())
        except ValueError:
            flash('Choose a valid date.', 'error')
    return query


def listing(title, query, columns, row, **context):
    model = query.column_descriptions[0]['entity']
    pagination = query.order_by(model.id.desc()).paginate(per_page=12, error_out=False)
    return render_template('list.html', title=title, columns=columns,
                           rows=[row(item) for item in pagination.items], pagination=pagination, **context)


def link(text, endpoint, **kwargs):
    return (text, url_for(f'pages.{endpoint}', **kwargs))


@pages.get('/')
def home():
    return render_template('home.html', title='Document similarity checker')


@pages.route('/register', methods=['GET', 'POST'])
def register():
    if not db.session.get(SystemSetting, 1).allow_registration:
        abort(403)
    if request.method == 'POST':
        username, password = value('username', 80).lower(), request.form.get('password', '')
        if not 3 <= len(username) <= 80 or not 10 <= len(password) <= 128:
            flash('Use a username of 3–80 characters and a password of 10–128 characters.', 'error')
        else:
            user = User(username=username, password=generate_password_hash(password))
            db.session.add(user)
            try:
                db.session.flush()
                audit('Registration', user, user=user)
                return finish('signin', 'Account created. Sign in to get started.')
            except IntegrityError:
                db.session.rollback()
                flash('That username is already taken.', 'error')
    return render_template('auth.html', title='Create an account', register=True)


@pages.route('/login', methods=['GET', 'POST'])
def signin():
    if request.method == 'POST':
        user = User.query.filter_by(username=value('username', 80).lower()).first()
        password = request.form.get('password', '')
        if user and user.enabled and len(password) <= 128 and check_password_hash(user.password, password):
            login_user(user)
            audit('Login')
            return finish('admin_dashboard' if user.admin else 'dashboard')
        audit('Login', status='Failed')
        db.session.commit()
        flash('The username or password could not be verified.', 'error')
    return render_template('auth.html', title='Sign in')


@pages.post('/logout')
@login_required
def signout():
    audit('Logout')
    logout_user()
    return finish('home')


def metrics():
    docs, checks, batches = scoped(Document), scoped(SimilarityAnalysis), scoped(BatchJob)
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in reversed(range(14))]

    def timeline(query, model):
        field = func.date(model.created)
        counts = dict(query.with_entities(field, func.count()).filter(field >= days[0]).group_by(field))
        return [counts.get(day, 0) for day in days]

    status = dict(docs.with_entities(Document.status, func.count()).group_by(Document.status))
    job_status = dict(batches.with_entities(BatchJob.status, func.count()).group_by(BatchJob.status))
    scores = [score for score, in checks.with_entities(SimilarityAnalysis.score)]
    check_counts = timeline(checks, SimilarityAnalysis)
    cards = {'Documents': docs.count(), 'Checks completed': len(scores),
             'Pending jobs': job_status.get('Pending', 0) + job_status.get('Processing', 0),
             'Reports': scoped(Report).count()}
    extra = {}
    if current_user.admin:
        cards.update({'Registered users': User.query.count(), 'Enabled users': User.query.filter_by(enabled=True).count(),
                      'Batch jobs': batches.count(), 'Failed jobs': job_status.get('Failed', 0) + job_status.get('Partial', 0)})
        occurrences = {}
        for result in SimilarityResult.query:
            source_id = result.metadata_snapshot.get('source_id')
            if source_id and result.score:
                key = (source_id, result.title)
                occurrences[key] = occurrences.get(key, 0) + 1
        extra = {'logs': AuditLog.query.order_by(AuditLog.id.desc()).limit(5).all(),
                 'users': User.query.order_by(User.id.desc()).limit(5).all(),
                 'top_sources': sorted(occurrences.items(), key=lambda row: -row[1])[:6]}
    cards['Average similarity'] = f'{sum(scores) / len(scores) if scores else 0:.1f}%'
    charts = {'labels': days, 'checks': check_counts, 'uploads': timeline(docs, Document),
              'statuses': status, 'scores': [sum(s < 30 for s in scores),
              sum(30 <= s < 70 for s in scores), sum(s >= 70 for s in scores)]}
    return dict(cards=cards, charts=charts, job_status=job_status,
                recent_docs=docs.order_by(Document.id.desc()).limit(5).all(),
                recent=checks.order_by(SimilarityAnalysis.id.desc()).limit(5).all(),
                notices=Notification.query.filter_by(owner_id=current_user.id).order_by(Notification.id.desc()).limit(5).all(),
                **extra)


@pages.get('/dashboard/')
@login_required
def dashboard():
    if current_user.admin:
        return redirect(url_for('pages.admin_dashboard'))
    return render_template('dashboard.html', title='My dashboard', **metrics())


@pages.get('/admin/')
@admin_required
def admin_dashboard():
    return render_template('dashboard.html', title='Admin dashboard', **metrics())


@pages.get('/analytics/')
@login_required
def analytics():
    return render_template('dashboard.html', title='Analytics', **metrics())


@pages.get('/documents/')
@login_required
def documents():
    query = scoped(Document).join(User, Document.owner)
    query = filtered(query, (Document.title, Document.filename), User.username if current_user.admin else None, Document.status, Document.category)
    return listing('Documents', query, ['Document', 'Filename', 'Owner', 'Category', 'Status', 'Uploaded'],
                   lambda d: [link(d.title, 'document', identifier=d.id), d.filename, d.owner.username,
                              d.category, d.status, d.created.date()], new=link('Upload documents', 'uploads'),
                   filters=['status', 'category', 'date'] + (['owner'] if current_user.admin else []))


@pages.route('/documents/upload/', methods=['GET', 'POST'])
@login_required
def uploads():
    if request.method == 'POST':
        files = [file for file in request.files.getlist('files') if file.filename]
        if not 1 <= len(files) <= 6:
            flash('Select one to six documents.', 'error')
        else:
            accepted = []
            for file in files:
                try:
                    with db.session.begin_nested():
                        doc = upload(file, value('title') if len(files) == 1 else '',
                                     value('category', 80), current_user)
                    accepted.append(doc)
                except (ValueError, IntegrityError) as exc:
                    flash(str(exc) if isinstance(exc, ValueError) else 'This file was already uploaded.', 'error')
            if accepted:
                if request.form.get('batch'):
                    return create_batch(accepted, 'Uploaded documents')
                return finish('documents', f'{len(accepted)} upload records saved.')
    return render_template('forms.html', title='Upload documents', fields=[
        ('files', 'PDF, DOCX or TXT files', 'multiple', None, True),
        ('title', 'Title for a single file', 'text', '', False),
        ('category', 'Category', 'text', '', False),
        ('batch', 'Create a batch from these files', 'checkbox', False, False)],
        button='Upload')


@pages.route('/documents/<int:identifier>/', methods=['GET', 'POST'])
@login_required
def document(identifier):
    doc = owned(Document, identifier)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'delete':
            audit('Document deletion', doc)
            db.session.delete(doc)
            return finish('documents', 'Document deleted. Analysis history remains available.')
        if action == 'rename' and value('title'):
            doc.title, doc.category = value('title'), value('category', 80)
            audit('Document updated', doc)
            return finish('document', 'Document updated.', identifier=identifier)
        if action == 'check' and doc.content:
            analysis = analyze(doc)
            return finish('result', 'Similarity report created.', identifier=analysis.id)
        abort(400)
    history = scoped(SimilarityAnalysis).filter_by(document_id=doc.id).order_by(SimilarityAnalysis.id.desc())
    return render_template('document.html', title=doc.title, doc=doc, history=history)


def create_batch(docs, title):
    job = BatchJob(owner_id=current_user.id, title=title, items=[
        BatchJobItem(document=d, title=d.title) for d in docs])
    db.session.add(job)
    db.session.flush()
    audit('Batch creation', job)
    return finish('batch', 'Batch queued.', identifier=job.id)


@pages.route('/batches/', methods=['GET', 'POST'])
@login_required
def batches():
    # Batch peers always belong to the same account.
    available = Document.query.filter_by(owner_id=current_user.id).order_by(Document.id.desc()).all()
    if request.method == 'POST':
        selected = set(request.form.getlist('documents'))
        docs = [doc for doc in available if str(doc.id) in selected]
        if docs and len(docs) == len(selected) and len(docs) <= 20:
            return create_batch(docs, value('title') or 'Document batch')
        flash('Select one to twenty of your documents.', 'error')
    return listing('Batch checking', scoped(BatchJob), ['Batch', 'Owner', 'Documents', 'Status', 'Created'],
                   lambda j: [link(j.title, 'batch', identifier=j.id), j.owner.username,
                              len(j.items), j.status, j.created.date()], available=available)


@pages.route('/batches/<int:identifier>/', methods=['GET', 'POST'])
@login_required
def batch(identifier):
    job = owned(BatchJob, identifier)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'cancel':
            changed = BatchJob.query.filter(BatchJob.id == job.id, BatchJob.status.in_(['Pending', 'Processing'])).update(
                {BatchJob.status: 'Cancelled'}, synchronize_session=False)
            if changed:
                BatchJobItem.query.filter_by(batch_id=job.id, status='Pending').update(
                    {BatchJobItem.status: 'Cancelled'}, synchronize_session=False)
                audit('Batch cancellation', job)
            return finish('batch', identifier=identifier)
        if action == 'retry' and current_user.admin and job.status in {'Partial', 'Failed'}:
            for item in job.items:
                if item.status == 'Failed':
                    item.status, item.error = 'Pending', ''
            job.status = 'Pending'
            audit('Batch retry', job)
            return finish('batch', identifier=identifier)
        if action != 'advance' or job.status not in ('Pending', 'Processing'):
            abort(400)
        advance_batch(job.id)
        return finish('batch', identifier=identifier)
    analyses = {a.batch_item_id: a for a in scoped(SimilarityAnalysis).join(
        BatchJobItem, BatchJobItem.id == SimilarityAnalysis.batch_item_id).filter(BatchJobItem.batch_id == job.id)}
    counts = {state: sum(i.status == state for i in job.items) for state in ('Pending', 'Processing', 'Completed', 'Failed', 'Cancelled')}
    return render_template('batch.html', title=job.title, job=job, analyses=analyses, counts=counts)


@pages.get('/batches/<int:identifier>/pdf')
@login_required
def batch_pdf(identifier):
    job = owned(BatchJob, identifier)
    analyses = scoped(SimilarityAnalysis).join(BatchJobItem, BatchJobItem.id == SimilarityAnalysis.batch_item_id)
    analyses = analyses.filter(BatchJobItem.batch_id == job.id).order_by(BatchJobItem.id).all()
    if not analyses:
        abort(404)
    output = pdf_report(job.title, analyses, f'Batch report #{job.id} · {job.created:%Y-%m-%d} UTC · {job.status}')
    audit('Batch report download', job)
    db.session.commit()
    return send_file(output, mimetype='application/pdf', as_attachment=True, download_name=f'batch-{job.id}.pdf')


@pages.get('/analysis/')
@pages.get('/history/')
@login_required
def history():
    query = filtered(scoped(SimilarityAnalysis).join(User, SimilarityAnalysis.owner), SimilarityAnalysis.title,
                     User.username if current_user.admin else None)
    return listing('Similarity analysis history', query, ['Analysis', 'Owner', 'Similarity', 'Date', 'Duration'],
                   lambda a: [link(a.title, 'result', identifier=a.id), a.owner.username,
                              f'{a.score}%', a.created.date(), f'{a.seconds:.3f}s'],
                   filters=['date'] + (['owner'] if current_user.admin else []))


@pages.get('/analysis/<int:identifier>/')
@login_required
def result(identifier):
    analysis = owned(SimilarityAnalysis, identifier)
    sources = sorted(analysis.results, key=lambda row: -row.score)
    bounds = []
    for start, end in sorted((m.start, m.end) for source in sources for m in source.segments):
        if bounds and start <= bounds[-1][1]:
            bounds[-1] = (bounds[-1][0], max(end, bounds[-1][1]))
        else:
            bounds.append((start, end))
    parts, previous = [], 0
    for start, end in bounds:
        parts.extend([(analysis.document_text[previous:start], False), (analysis.document_text[start:end], True)])
        previous = end
    parts.append((analysis.document_text[previous:], False))
    algorithms = ', '.join(METHODS[key] for key in analysis.methods['algorithms'])
    return render_template('result.html', title=analysis.title, analysis=analysis, sources=sources,
                           parts=parts, algorithms=algorithms, disclaimer=DISCLAIMER)


@pages.get('/reports/')
@login_required
def reports():
    query = scoped(Report).join(SimilarityAnalysis).join(User, Report.owner)
    query = filtered(query, SimilarityAnalysis.title, User.username if current_user.admin else None)
    if request.args.get('minimum'):
        try:
            query = query.filter(SimilarityAnalysis.score >= float(request.args['minimum']))
        except ValueError:
            flash('Enter a numeric minimum similarity.', 'error')
    return listing('Reports', query, ['Report', 'Document', 'Owner', 'Similarity', 'Created'],
                   lambda r: [link(f'#{r.id}', 'report', identifier=r.id), r.analysis.title,
                              r.owner.username, f'{r.analysis.score}%', r.created.date()],
                   filters=['date', 'minimum'] + (['owner'] if current_user.admin else []))


@pages.route('/reports/<int:identifier>/', methods=['GET', 'POST'])
@login_required
def report(identifier):
    row = owned(Report, identifier)
    if request.method == 'POST':
        audit('Report deleted', row)
        db.session.delete(row)
        return finish('reports', 'Report deleted.')
    return render_template('report.html', title=f'Report #{row.id}', row=row, disclaimer=DISCLAIMER)


@pages.get('/reports/<int:identifier>/pdf')
@login_required
def report_pdf(identifier):
    row = owned(Report, identifier)
    output = pdf_report('Document similarity report', [row.analysis], f'Report #{row.id}')
    audit('Report download', row)
    db.session.commit()
    return send_file(output, mimetype='application/pdf', as_attachment=True, download_name=f'report-{row.id}.pdf')


@pages.get('/sources/')
@admin_required
def sources():
    query = filtered(Source.query, Source.title, category=Source.category)
    counts = {}
    for result in SimilarityResult.query:
        identifier = result.metadata_snapshot.get('source_id')
        counts[identifier] = counts.get(identifier, 0) + 1
    return listing('Source library', query, ['Title', 'Author', 'Type', 'Category', 'Matches'],
                   lambda s: [link(s.title, 'source', identifier=s.id), s.author, s.kind,
                              s.category, counts.get(s.id, 0)], filters=['category'],
                   new=link('Add source', 'source', identifier=0))


@pages.route('/sources/<int:identifier>/', methods=['GET', 'POST'])
@admin_required
def source(identifier):
    row = db.get_or_404(Source, identifier) if identifier else Source()
    specs = [
        ('title', 'Title', 'text', 160), ('author', 'Author', 'text', 120),
        ('kind', 'Source type', 'text', 80), ('category', 'Category', 'text', 80),
        ('url', 'Reference URL', 'url', 500), ('published', 'Publication date', 'date', 10),
        ('description', 'Description', 'textarea', 4000), ('text', 'Reference text', 'textarea', 40000),
    ]
    if request.method == 'POST':
        if request.form.get('action') == 'delete' and identifier:
            audit('Source deletion', row)
            db.session.delete(row)
            return finish('sources', 'Source deleted.')
        try:
            data = {name: value(name, size) for name, _, _, size in specs}
            if not data['title']:
                raise ValueError('Enter a source title.')
            if data['published']:
                date.fromisoformat(data['published'])
            if data['url']:
                parsed = urlsplit(data['url'])
                if parsed.scheme not in ('https', 'http') or not parsed.hostname:
                    raise ValueError('Use an HTTP or HTTPS reference URL.')
            file = request.files.get('reference')
            if file and file.filename:
                data['text'] = extract(file.filename, file.read(10 * 1024**2 + 1))
            if not any(char.isalnum() for char in data['text']):
                raise ValueError('Enter reference text or upload a readable file.')
            for name, result in data.items():
                setattr(row, name, result)
            db.session.add(row)
            db.session.flush()
            audit('Source updated' if identifier else 'Source created', row)
            return finish('sources', 'Source saved.')
        except ValueError as exc:
            flash(str(exc), 'error')
    fields = [(name, label, kind, getattr(row, name, '') or '', name == 'title')
              for name, label, kind, _ in specs]
    fields.append(('reference', 'Upload reference text from a file', 'file', None, False))
    return render_template('forms.html', title=row.title or 'Add reference source', fields=fields,
                           button='Save source', delete=bool(identifier))


@pages.route('/notifications/', methods=['GET', 'POST'])
@login_required
def notifications():
    query = Notification.query.filter_by(owner_id=current_user.id)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'mark-all':
            query.filter_by(read=False).update({Notification.read: True})
        elif action in {'read', 'delete'}:
            notice = query.filter_by(id=request.form.get('id', type=int)).first_or_404()
            if action == 'delete':
                db.session.delete(notice)
            else:
                notice.read = True
        else:
            abort(400)
        return finish('notifications')
    query = filtered(query, Notification.title, category=Notification.category)
    pagination = query.order_by(Notification.id.desc()).paginate(per_page=12, error_out=False)
    return render_template('notifications.html', title='Notifications', pagination=pagination)


@pages.route('/admin/announcements/', methods=['GET', 'POST'])
@admin_required
def announcements():
    if request.method == 'POST':
        title, message = value('title'), value('message', 4000)
        username = value('username', 80).lower()
        category = value('category', 40)
        targets = User.query.filter_by(username=username).all() if username else User.query.filter_by(enabled=True).all()
        if title and message and targets and category in ('Announcement', 'System update'):
            for user in targets:
                notify(user.id, title, message, category)
            audit('Announcement sent', description=f'{category}: {title} ({len(targets)} recipients)')
            return finish('announcements', f'Sent to {len(targets)} recipients.')
        flash('Enter a title, message, and valid recipient or leave recipient blank.', 'error')
    return listing('Announcements', Notification.query.filter(Notification.category.in_(['Announcement', 'System update'])),
                   ['Notice', 'Recipient', 'Category', 'Read', 'Sent'],
                   lambda n: [n.title, n.owner.username, n.category, 'Yes' if n.read else 'No', n.created.date()],
                   fields=[('title', 'Title', 'text', '', True), ('message', 'Message', 'textarea', '', True),
                           ('username', 'Recipient username (leave blank for all enabled users)', 'text', '', False),
                           ('category', 'Category', 'select', ('Announcement', 'System update'), True)],
                   button='Send announcement')


@pages.route('/feedback/', methods=['GET', 'POST'])
@login_required
def feedback():
    if request.method == 'POST':
        title, category, message = value('title'), value('category', 40), value('message', 5000)
        if title and message and category in FEEDBACK:
            row = Feedback(owner_id=current_user.id, title=title, category=category, message=message)
            db.session.add(row)
            db.session.flush()
            audit('Feedback submitted', row)
            return finish('feedback_detail', 'Feedback submitted.', identifier=row.id)
        flash('Enter a title, category, and message.', 'error')
    query = filtered(scoped(Feedback), Feedback.title, status=Feedback.status, category=Feedback.category)
    return listing('Feedback and support', query, ['Title', 'Category', 'Status', 'Priority', 'Submitted'],
                   lambda f: [link(f.title, 'feedback_detail', identifier=f.id), f.category,
                              f.status, f.priority, f.created.date()],
                   filters=['status', 'category'],
                   fields=[('title', 'Title', 'text', '', True), ('category', 'Category', 'select', FEEDBACK, True),
                           ('message', 'Message', 'textarea', '', True)], button='Submit feedback')


@pages.route('/feedback/<int:identifier>/', methods=['GET', 'POST'])
@login_required
def feedback_detail(identifier):
    row = owned(Feedback, identifier)
    if request.method == 'POST':
        if not current_user.admin:
            abort(403)
        status, priority, response = value('status'), value('priority'), value('response', 5000)
        if status not in STATES or priority not in PRIORITIES:
            abort(400)
        row.status, row.priority, row.response = status, priority, response
        notify(row.owner_id, 'Feedback updated', f'{row.title} · {status}', 'Feedback')
        audit('Feedback updated', row, description=status)
        return finish('feedback_detail', 'Feedback updated.', identifier=row.id)
    return render_template('feedback.html', title=row.title, row=row, states=STATES, priorities=PRIORITIES)


@pages.get('/admin/users/')
@admin_required
def users():
    return listing('Users', filtered(User.query, User.username), ['Username', 'Role', 'Status', 'Registered'],
                   lambda u: [link(u.username, 'user_detail', identifier=u.id), 'Admin' if u.admin else 'User',
                              'Enabled' if u.enabled else 'Disabled', u.created.date()])


@pages.route('/admin/users/<int:identifier>/', methods=['GET', 'POST'])
@admin_required
def user_detail(identifier):
    user = db.get_or_404(User, identifier)
    if request.method == 'POST':
        if user.admin:
            abort(403)
        user.enabled = request.form.get('enabled') == 'on'
        audit('User access updated', user, description='Enabled' if user.enabled else 'Disabled')
        return finish('users', 'User access updated.')
    fields = [('enabled', 'Allow sign in', 'checkbox', user.enabled, False)] if not user.admin else []
    return render_template('forms.html', title=user.username, fields=fields, button='Save access',
                           explanation='Administrator account' if user.admin else 'Enable or disable this account.')


@pages.route('/profile/', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        username = value('username', 80).lower()
        previous = request.form.get('current_password', '')
        new_password = request.form.get('password', '')
        if (len(previous) > 128 or not check_password_hash(current_user.password, previous)
                or len(username) < 3 or new_password and not 10 <= len(new_password) <= 128):
            flash('Verify your current password and use a new password of 10–128 characters if changing it.', 'error')
        else:
            current_user.username = username
            if new_password:
                current_user.password = generate_password_hash(new_password)
            try:
                audit('Profile updated', current_user)
                return finish('profile', 'Profile saved.')
            except IntegrityError:
                db.session.rollback()
                flash('That username is already taken.', 'error')
    return render_template('forms.html', title='Profile', fields=[
        ('username', 'Username', 'text', current_user.username, True),
        ('current_password', 'Current password', 'password', '', True),
        ('password', 'New password', 'password', '', False)], button='Save profile')


def audit_query():
    return filtered(AuditLog.query, AuditLog.action, AuditLog.username, AuditLog.status)


@pages.get('/admin/audit/')
@admin_required
def audit_log():
    return listing('Audit log', audit_query(), ['ID', 'User', 'Role', 'Action', 'Status', 'Timestamp'],
                   lambda a: [link(a.id, 'audit_detail', identifier=a.id), a.username, a.role,
                              a.action, a.status, a.created], filters=['owner', 'status', 'date'],
                   new=('Export CSV', url_for('pages.audit_csv', **request.args)))


@pages.get('/admin/audit/<int:identifier>/')
@admin_required
def audit_detail(identifier):
    row = db.get_or_404(AuditLog, identifier)
    return render_template('audit.html', title=f'Audit entry #{row.id}', row=row)


@pages.get('/admin/audit.csv')
@admin_required
def audit_csv():
    output = StringIO()
    writer = csv.writer(output)
    fields = ('id', 'user_id', 'username', 'role', 'action', 'description', 'related', 'created', 'status', 'ip')
    writer.writerow(fields)
    for row in audit_query().order_by(AuditLog.id.desc()).yield_per(200):
        values = [str(getattr(row, key) or '') for key in fields]
        writer.writerow(["'" + v if v.lstrip().startswith(('=', '+', '-', '@')) or v.startswith(('\t', '\r', '\n')) else v
                         for v in values])
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=audit.csv'})


@pages.route('/admin/models/', methods=['GET', 'POST'])
@admin_required
def model_management():
    row = db.session.get(ModelConfiguration, 1)
    trials = None
    if request.method == 'POST':
        methods = list(dict.fromkeys(request.form.getlist('methods')))
        threshold = request.form.get('threshold', type=float)
        size = request.form.get('ngram_size', type=int)
        if not methods or not set(methods) <= METHODS.keys() or threshold is None or not .1 <= threshold <= 1 or size not in range(2, 7):
            flash('Choose at least one method, a threshold from 0.1 to 1, and an n-gram size from 2 to 6.', 'error')
        elif request.form.get('action') == 'test':
            left, right = value('left', 6000), value('right', 6000)
            if left and right:
                trials = [(METHODS[method], compare(left, right, [method], threshold, size)[0]) for method in methods]
                trials.append(('Combined', compare(left, right, methods, threshold, size)[0]))
            else:
                flash('Enter text in both sample fields.', 'error')
        else:
            row.methods, row.threshold, row.ngram_size = methods, threshold, size
            db.session.add(ModelVersion(username=current_user.username, snapshot={
                'methods': methods, 'threshold': threshold, 'ngram_size': size,
            }))
            audit('Model configuration changed', row)
            return finish('model_management', 'Configuration saved.')
    return render_template('models.html', title='Model management', row=row, trials=trials,
                           versions=ModelVersion.query.order_by(ModelVersion.id.desc()).paginate(per_page=12, error_out=False))


@pages.route('/admin/settings/', methods=['GET', 'POST'])
@admin_required
def settings():
    row = db.session.get(SystemSetting, 1)
    if request.method == 'POST':
        size = request.form.get('max_upload_mb', type=int)
        if size not in range(1, 11):
            flash('Choose an upload limit from 1 to 10 MB.', 'error')
        else:
            row.max_upload_mb, row.allow_registration = size, request.form.get('allow_registration') == 'on'
            audit('Settings changed', row)
            return finish('settings', 'Settings saved.')
    return render_template('forms.html', title='System settings', fields=[
        ('max_upload_mb', 'Maximum file size in MB (1–10)', 'number', row.max_upload_mb, True),
        ('allow_registration', 'Allow public registration', 'checkbox', row.allow_registration, False)],
        button='Save settings')
