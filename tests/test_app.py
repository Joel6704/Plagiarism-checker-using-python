import io
import re
import unittest

from docx import Document as WordDocument
from reportlab.pdfgen.canvas import Canvas

from app import create_app
from app.models import AuditLog, BatchJob, Document, ModelVersion, Notification, Report, db
from app.services import compare, extract

TEXT = 'Rain gardens capture stormwater runoff from roofs and paved surfaces.'


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-only',
                               'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})
        self.client = self.app.test_client()
        self.admin = self.app.test_client()
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=['create-admin', 'reviewer'], input='reviewer-pass\nreviewer-pass\n')
        self.assertEqual(result.exit_code, 0, result.output)
        runner.invoke(args=['seed-demo'])
        self.register(self.client, 'joel')

    def post(self, client, url, **data):
        page = client.get(url)
        token = re.search(rb'name="csrf-token" content="([^"]+)', page.data)
        self.assertIsNotNone(token, url)
        return client.post(url, data={'csrf_token': token[1].decode(), **data})

    def login(self, client, username, password):
        return self.post(client, '/login', username=username, password=password)

    def register(self, client, username):
        self.assertEqual(self.post(client, '/register', username=username, password='example-pass').status_code, 302)
        self.assertEqual(self.login(client, username, 'example-pass').location, '/dashboard/')

    def upload(self, text=TEXT, name='draft.txt'):
        return self.post(self.client, '/documents/upload/', files=(io.BytesIO(text.encode()), name))

    def test_login_registration_and_access(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/admin/').status_code, 403)
        self.assertEqual(self.client.post('/logout').status_code, 400)
        self.assertEqual(self.login(self.admin, 'reviewer', 'reviewer-pass').location, '/admin/')
        for path in ('/admin/', '/admin/users/', '/sources/', '/admin/audit/', '/admin/models/',
                     '/admin/settings/', '/admin/announcements/', '/analytics/'):
            with self.subTest(path=path):
                self.assertEqual(self.admin.get(path).status_code, 200)

    def test_extraction_and_similarity(self):
        with self.app.app_context():
            word = WordDocument()
            word.add_paragraph(TEXT)
            buffer = io.BytesIO()
            word.save(buffer)
            self.assertEqual(extract('draft.docx', buffer.getvalue()), TEXT)
            buffer = io.BytesIO()
            pdf = Canvas(buffer)
            pdf.drawString(20, 50, TEXT)
            pdf.save()
            self.assertEqual(extract('draft.pdf', buffer.getvalue()), TEXT)
            with self.assertRaises(ValueError):
                extract('fake.pdf', TEXT.encode())
        score, segments = compare(TEXT, TEXT, ['tfidf', 'ngram', 'sentence', 'sequence'], .72, 3)
        self.assertEqual(score, 100)
        self.assertEqual(len(segments), 1)
        self.assertEqual(compare(TEXT, 'Volcanic rocks cool.', ['tfidf', 'sentence'], .72, 3)[0], 0)

    def test_upload_report_notifications_and_private_access(self):
        self.assertIn(b'Choose a PDF', self.upload(name='draft.exe').data)
        self.assertEqual(self.upload().status_code, 302)
        self.assertIn(b'already been uploaded', self.upload().data)
        result = self.post(self.client, '/documents/1/', action='check')
        self.assertEqual(result.location, '/analysis/1/')
        self.assertIn(b'<mark>', self.client.get(result.location).data)
        self.assertEqual(self.client.get('/reports/').status_code, 200)
        self.assertEqual(self.client.get('/reports/1/').status_code, 200)
        self.assertTrue(self.client.get('/reports/1/pdf').data.startswith(b'%PDF'))
        other = self.app.test_client()
        self.register(other, 'other')
        for url in ('/documents/1/', '/analysis/1/', '/reports/1/pdf'):
            self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(self.post(self.client, '/notifications/', action='mark-all').status_code, 302)
        self.assertEqual(self.post(self.client, '/documents/1/', action='delete').status_code, 302)
        self.assertEqual(self.client.get('/analysis/1/').status_code, 200)
        with self.app.app_context():
            self.assertEqual(Notification.query.filter_by(read=False).count(), 0)
            self.assertEqual(Document.query.count(), 0)
            self.assertEqual(Report.query.count(), 1)
            self.assertIsNotNone(AuditLog.query.filter_by(action='Report download').first())

    def test_batch_success_failure_and_cancellation(self):
        self.upload()
        self.upload('Other content about trees.', 'other.txt')
        self.upload('Invalid PDF bytes', 'bad.pdf')
        batch = self.post(self.client, '/batches/', title='Review', documents=['1', '2', '3'])
        self.assertEqual(batch.location, '/batches/1/')
        for _ in range(3):
            self.assertEqual(self.post(self.client, batch.location, action='advance').status_code, 302)
        self.assertIn(b'Partial', self.client.get(batch.location).data)
        self.assertTrue(self.client.get('/batches/1/pdf').data.startswith(b'%PDF'))
        self.post(self.client, '/batches/', documents=['1'])
        self.post(self.client, '/batches/2/', action='cancel')
        with self.app.app_context():
            self.assertEqual(db.session.get(BatchJob, 2).status, 'Cancelled')

    def test_background_batch_worker(self):
        self.upload()
        self.post(self.client, '/batches/', documents=['1'])
        result = self.app.test_cli_runner().invoke(args=['work-batches', '--once'])
        self.assertEqual(result.exit_code, 0, result.exception)
        with self.app.app_context():
            self.assertEqual(db.session.get(BatchJob, 1).status, 'Completed')

    def test_admin_source_feedback_announcements_and_models(self):
        self.login(self.admin, 'reviewer', 'reviewer-pass')
        self.assertEqual(self.post(self.admin, '/sources/0/', title='Sample', text=TEXT).status_code, 302)
        self.assertIn(b'Sample', self.admin.get('/sources/').data)
        self.assertEqual(self.post(self.admin, '/sources/2/', action='delete').status_code, 302)
        self.post(self.client, '/feedback/', title='Review request', category='Report Issue', message='Please review.')
        self.assertEqual(self.post(self.client, '/feedback/1/', status='Reviewed').status_code, 403)
        self.assertEqual(self.post(self.admin, '/feedback/1/', status='Resolved', priority='High', response='Reviewed.').status_code, 302)
        self.assertIn(b'Reviewed.', self.client.get('/feedback/1/').data)
        self.assertEqual(self.post(self.admin, '/admin/announcements/', title='System notice', message='Welcome',
                                   username='joel', category='Announcement').status_code, 302)
        self.assertIn(b'System notice', self.client.get('/notifications/').data)
        status = self.post(self.admin, '/admin/models/', methods=['sentence'], threshold='.85', ngram_size='4')
        self.assertEqual(status.status_code, 302)
        trial = self.post(self.admin, '/admin/models/', methods=['sentence'], threshold='.85',
                          ngram_size='4', action='test', left=TEXT, right=TEXT)
        self.assertIn(b'100%', trial.data)
        with self.app.app_context():
            self.assertEqual(ModelVersion.query.count(), 2)
        self.assertIn(b'Model configuration changed', self.admin.get('/admin/audit.csv').data)


if __name__ == '__main__':
    unittest.main()
