from flask import current_app

from .models import BatchJob, db
from .services import analyze, audit, notify

READY = ('Pending', 'Processing')


def advance_batch(identifier):
    """Process one item while serializing job mutations through SQLite."""
    claimed = BatchJob.query.filter(BatchJob.id == identifier, BatchJob.status.in_(READY)).update(
        {BatchJob.status: 'Processing'}, synchronize_session=False)
    if not claimed:
        return False

    job = db.session.get(BatchJob, identifier, populate_existing=True)
    item = next((entry for entry in job.items if entry.status == 'Pending'), None)
    if item:
        if not item.document or not item.document.content:
            item.status = 'Failed'
            item.error = 'Document was deleted or contains no readable text.'
        else:
            try:
                with db.session.begin_nested():
                    peers = [entry.document for entry in job.items if entry.document]
                    analyze(item.document, peers, item)
                    item.status = 'Completed'
            except Exception:
                current_app.logger.exception('Batch item %s failed', item.id)
                item.status, item.error = 'Failed', 'Similarity analysis failed.'

    statuses = {entry.status for entry in job.items}
    if 'Pending' not in statuses:
        if 'Failed' in statuses:
            job.status = 'Partial' if 'Completed' in statuses else 'Failed'
        else:
            job.status = 'Completed'
        notify(job.owner_id, 'Batch finished', f'{job.title} · {job.status}', 'Batch')
        audit('Batch finished', job, status=job.status)
    return True
