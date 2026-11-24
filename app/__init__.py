import os
import secrets
import time
from pathlib import Path
from sqlite3 import Connection

import click
from flask import Flask, render_template
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash

from .batches import READY, advance_batch
from .models import BatchJob, ModelConfiguration, ModelVersion, Notification, Source, SystemSetting, User, db
from .services import METHODS
from .views import pages

login = LoginManager()


@event.listens_for(Engine, 'connect')
def sqlite_foreign_keys(connection, _):
    if isinstance(connection, Connection):
        connection.execute('PRAGMA foreign_keys=ON')


@login.user_loader
def user_loader(identifier):
    user = db.session.get(User, int(identifier))
    return user if user and user.enabled else None


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///checker.sqlite3',
        SECRET_KEY=os.environ.get('CHECKER_SECRET_KEY'),
        MAX_CONTENT_LENGTH=65 * 1024**2,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('CHECKER_SECURE_COOKIES') == '1',
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if not app.secret_key:
        key = Path(app.instance_path, 'session.key')
        try:
            with open(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
                output.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.secret_key = key.read_text().strip()
    db.init_app(app)
    login.init_app(app)
    login.login_view = 'pages.signin'
    CSRFProtect(app)
    app.register_blueprint(pages)

    with app.app_context():
        db.create_all()
        if not db.session.get(ModelConfiguration, 1):
            methods = ['tfidf', 'ngram', 'sentence']
            db.session.add(ModelConfiguration(id=1, methods=methods))
            db.session.add(ModelVersion(username='System', snapshot={
                'methods': methods, 'threshold': .72, 'ngram_size': 3,
            }))
        if not db.session.get(SystemSetting, 1):
            db.session.add(SystemSetting(id=1))
        db.session.commit()

    @app.context_processor
    def globals_for_templates():
        unread = Notification.query.filter_by(owner_id=current_user.id, read=False).count() if current_user.is_authenticated else 0
        return {'unread': unread, 'method_names': METHODS}

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(error):
        return render_template('error.html', title=error.name, explanation=error.description), error.code

    @app.cli.command('create-admin')
    @click.argument('username')
    @click.password_option()
    def create_admin(username, password):
        username = username.strip().lower()
        if len(password) < 10 or not 3 <= len(username) <= 80:
            raise click.ClickException('Use a 3–80 character username and a password of at least 10 characters.')
        if User.query.filter_by(username=username).first():
            raise click.ClickException('That username already exists.')
        db.session.add(User(username=username, password=generate_password_hash(password), admin=True))
        db.session.commit()
        click.echo(f'Administrator {username} created.')

    @app.cli.command('seed-demo')
    def seed_demo():
        if not Source.query.filter_by(title='Demo reference · Rain gardens').first():
            db.session.add(Source(title='Demo reference · Rain gardens', author='Demo collection',
                kind='Example', category='Environment',
                text='Rain gardens capture stormwater runoff from roofs and paved surfaces. Native plants help water soak into the soil.'))
            db.session.commit()
        click.echo('Demo source is available. Try uploading a TXT file with the sentence: Rain gardens capture stormwater runoff from roofs and paved surfaces.')

    @app.cli.command('work-batches')
    @click.option('--once', is_flag=True, help='Process queued jobs and exit once the queue is empty.')
    def work_batches(once):
        click.echo('Processing batch jobs.')
        while True:
            job = BatchJob.query.filter(BatchJob.status.in_(READY)).order_by(BatchJob.id).first()
            if job:
                advance_batch(job.id)
                db.session.commit()
            else:
                db.session.rollback()
                if once:
                    break
                time.sleep(2)

    return app
