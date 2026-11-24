import hashlib
import math
import re
from collections import Counter
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from time import perf_counter
from xml.sax.saxutils import escape
from zipfile import ZipFile, is_zipfile

from docx import Document as WordDocument
from flask import has_request_context, request
from flask_login import current_user
from pypdf import PdfReader
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .models import (
    AuditLog, Document, DocumentText, MatchSegment, ModelConfiguration,
    Notification, Report, SimilarityAnalysis, SimilarityResult, Source,
    SystemSetting, db,
)

METHODS = {
    'tfidf': 'TF-IDF cosine',
    'ngram': 'Word n-gram overlap',
    'sentence': 'Sentence matching',
    'sequence': 'Token sequence matching',
}
DISCLAIMER = 'Similarity findings require human review. Comparisons cover imported references and selected batch documents only.'
STOP = frozenset('a an the and or of in to for with on is are was were by'.split())


def tokens(text):
    return re.findall(r'[^\W_]+', text.casefold())


def extract(filename, data):
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.txt', '.pdf', '.docx'}:
        raise ValueError('Choose a PDF, DOCX, or TXT file.')
    limit = db.session.get(SystemSetting, 1).max_upload_mb * 1024**2
    if not data or len(data) > limit:
        raise ValueError('The file is empty or exceeds the upload limit.')
    try:
        stream = BytesIO(data)
        if suffix == '.txt':
            text = data.decode('utf-8-sig')
        elif suffix == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise ValueError
            pdf = PdfReader(stream)
            if pdf.is_encrypted or len(pdf.pages) > 150:
                raise ValueError
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        else:
            if not is_zipfile(stream):
                raise ValueError
            with ZipFile(stream) as archive:
                if sum(item.file_size for item in archive.infolist()) > 25 * 1024**2:
                    raise ValueError
            word = WordDocument(stream)
            lines = [p.text for p in word.paragraphs]
            lines.extend(cell.text for table in word.tables for row in table.rows for cell in row.cells)
            text = '\n'.join(lines)
    except Exception as exc:
        raise ValueError('The file could not be read. Use an unencrypted PDF, valid DOCX, or UTF-8 TXT.') from exc
    text = text.strip()
    if not tokens(text) or '\x00' in text or len(text) > 40_000:
        raise ValueError('The file needs readable text and must contain at most 40,000 characters.')
    return text


def audit(action, obj=None, description='', status='Success', user=None):
    actor = user or (current_user if has_request_context() else None)
    db.session.add(AuditLog(
        user_id=actor.id if actor and actor.is_authenticated else None,
        username=actor.username if actor and actor.is_authenticated else 'Anonymous',
        role=('Admin' if actor.admin else 'User') if actor and actor.is_authenticated else 'Guest',
        action=action, related=f'{type(obj).__name__} #{obj.id}' if obj else '',
        description=description, status=status, ip=(request.remote_addr or '') if has_request_context() else '',
    ))


def notify(user_id, title, message, category):
    db.session.add(Notification(owner_id=user_id, title=title, message=message, category=category))


def upload(file, title, category, owner):
    filename = file.filename.replace('\\', '/').split('/')[-1][:255]
    if Path(filename).suffix.lower() not in {'.pdf', '.docx', '.txt'}:
        raise ValueError('Choose a PDF, DOCX, or TXT file.')
    limit = db.session.get(SystemSetting, 1).max_upload_mb * 1024**2
    data = file.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError('The file is empty or exceeds the upload limit.')
    digest = hashlib.sha256(data).hexdigest()
    if Document.query.filter_by(owner_id=owner.id, digest=digest).first():
        raise ValueError(f'{filename} has already been uploaded.')
    doc = Document(owner=owner, filename=filename, title=title or filename[:160],
                   category=category, digest=digest, status='Processing')
    db.session.add(doc)
    try:
        doc.content = DocumentText(text=extract(filename, data))
        doc.status = 'Completed'
    except ValueError as exc:
        doc.status, doc.error = 'Failed', str(exc)
    db.session.flush()
    audit('Document upload', doc, status=doc.status)
    notify(owner.id, f'Document {doc.status.lower()}', doc.error or doc.title, 'Documents')
    return doc


def tfidf(left, right):
    counts = [Counter(word for word in words if word not in STOP) for words in (left, right)]
    weights = {w: 1 + math.log(3 / (1 + sum(w in c for c in counts)))
               for w in counts[0].keys() | counts[1].keys()}
    vectors = [{w: n * weights[w] for w, n in c.items()} for c in counts]
    a, b = vectors
    norm = math.sqrt(sum(v*v for v in a.values()) * sum(v*v for v in b.values()))
    return sum(a[w] * b[w] for w in a.keys() & b.keys()) / norm if norm else 0


def ngrams(words, size):
    return set(zip(*(words[i:] for i in range(size)))) if len(words) >= size else set()


def sentences(text):
    parts = []
    for match in re.finditer(r'[^.!?\n]+[.!?]*', text):
        raw = match.group()
        if raw.strip():
            parts.append((match.start() + len(raw) - len(raw.lstrip()), match.end(), raw.strip()))
    return parts


def compare(left, right, methods, threshold, ngram_size):
    a, b = tokens(left), tokens(right)
    scores, matches = [], []
    if 'tfidf' in methods:
        scores.append(tfidf(a, b))
    if 'ngram' in methods:
        x, y = ngrams(a, ngram_size), ngrams(b, ngram_size)
        scores.append(len(x & y) / len(x | y) if x or y else 0)
    if 'sequence' in methods:
        scores.append(SequenceMatcher(None, a, b).ratio())
    if 'sentence' in methods:
        left_parts = sentences(left)
        exact = {}
        index = {}
        for _, _, text in sentences(right):
            key = tuple(tokens(text))
            if not key:
                continue
            exact[key] = text
            for word in set(key) - STOP:
                index.setdefault(word, []).append(key)
        covered = 0
        total = 0
        for start, end, text in left_parts:
            key = tuple(tokens(text))
            total += len(key)
            if not key:
                continue
            if key in exact:
                best_score, best_text = 1.0, exact[key]
            else:
                counts = Counter(candidate for word in set(key) - STOP for candidate in index.get(word, ()))
                best_score, best_text = max(
                    ((SequenceMatcher(None, key, candidate).ratio(), exact[candidate])
                     for candidate, _ in counts.most_common(24)),
                    default=(0, ''),
                )
            if best_score >= threshold and best_score > 0:
                matches.append((start, end, best_text, best_score * 100))
                covered += len(key)
        scores.append(covered / total if total else 0)
    return round(100 * min(1, sum(scores) / len(scores)), 2), matches


def analyze(doc, peers=(), item=None):
    started = perf_counter()
    config = db.session.get(ModelConfiguration, 1)
    methods = config.methods
    audit('Check initiated', doc)
    notify(doc.owner_id, 'Similarity check started', doc.title, 'Analysis')
    candidates = [(src.title, src.text, {
        'source_id': src.id, 'author': src.author, 'category': src.category,
        'type': src.kind, 'url': src.url, 'published': src.published,
        'description': src.description,
    }) for src in Source.query.order_by(Source.id)]
    candidates.extend((peer.title, peer.content.text, {'type': 'Batch document'})
                      for peer in peers if peer.id != doc.id and peer.content)
    analysis = SimilarityAnalysis(
        owner_id=doc.owner_id, document_id=doc.id, title=doc.title, filename=doc.filename,
        document_text=doc.content.text, score=0, seconds=0,
        methods={'algorithms': methods, 'threshold': config.threshold,
                 'ngram_size': config.ngram_size, 'sources_checked': len(candidates)},
        batch_item_id=item.id if item else None,
    )
    for title, reference, metadata in candidates:
        score, segments = compare(analysis.document_text, reference, methods,
                                  config.threshold, config.ngram_size)
        analysis.score = max(analysis.score, score)
        if score or segments:
            result = SimilarityResult(title=title, score=score, metadata_snapshot=metadata)
            result.segments = [MatchSegment(start=s, end=e, excerpt=t, score=v)
                               for s, e, t, v in segments]
            analysis.results.append(result)
    analysis.seconds = perf_counter() - started
    analysis.report = Report(owner_id=doc.owner_id)
    db.session.add(analysis)
    db.session.flush()
    audit('Analysis completed', analysis)
    audit('Report generated', analysis.report)
    notify(doc.owner_id, 'Similarity check completed', doc.title, 'Analysis')
    notify(doc.owner_id, 'Report available', f'Report #{analysis.report.id} can be downloaded.', 'Reports')
    return analysis


def pdf_report(title, analyses, report_label):
    output = BytesIO()
    styles = getSampleStyleSheet()
    flow = [Paragraph(escape(title), styles['Title']),
            Paragraph(escape(report_label), styles['Normal']),
            Paragraph(escape(DISCLAIMER), styles['Normal'])]
    for analysis in analyses:
        flow.extend([
            Spacer(1, 14),
            Paragraph(escape(analysis.title), styles['Heading2']),
            Paragraph(escape(
                f'File {analysis.filename} | Document #{analysis.document_id or "deleted"} | '
                f'Uploaded by {analysis.owner.username} | Analysis #{analysis.id} | '
                f'{analysis.created:%Y-%m-%d %H:%M} UTC | {analysis.score}% similarity | '
                f'{analysis.seconds:.3f}s'
            ), styles['Normal']),
            Paragraph(escape('Methods: ' + ', '.join(METHODS[m] for m in analysis.methods['algorithms'])
                             + f'. References checked: {analysis.methods["sources_checked"]}.'),
                      styles['Normal']),
        ])
        for result in sorted(analysis.results, key=lambda row: -row.score):
            meta = ' | '.join(str(value) for key, value in result.metadata_snapshot.items()
                              if key != 'source_id' and value)
            flow.append(Paragraph(escape(f'{result.title} · {result.score}% · {meta}'), styles['Heading3']))
            for segment in result.segments:
                original = analysis.document_text[segment.start:segment.end]
                flow.append(Paragraph(escape(
                    f'{segment.score:.1f}% · Submitted: {original} · Reference: {segment.excerpt}'
                ), styles['Normal']))
    SimpleDocTemplate(output, title=title).build(flow)
    output.seek(0)
    return output
