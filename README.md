# Similarity Lab

A local document similarity checker built with Python, Flask, SQLAlchemy, SQLite, Flask-Login, Bootstrap 5, and Chart.js. It compares readable PDF, DOCX, and UTF-8 TXT uploads with imported references and other documents selected in the same batch. Findings require human review.

## Run on Windows 11

Install Python 3.11 or later. Open PowerShell inside this project folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app run create-admin admin
.\.venv\Scripts\python.exe -m flask --app run seed-demo
.\.venv\Scripts\python.exe -m flask --app run run --host 127.0.0.1 --port 5000
```

Enter your chosen administrator password when prompted. The command requires at least 10 characters. You can skip `seed-demo` for an empty source library. SQLite tables and a random local session key are created on first launch in the private `instance/` directory. Keep that directory for future sessions and exclude it from your Git commits.

On macOS and Linux, create the environment with `python3 -m venv .venv` and replace `.\.venv\Scripts\python.exe` in the remaining commands with `.venv/bin/python`.

| Page | Address | Access |
| --- | --- | --- |
| Home | http://127.0.0.1:5000/ | Anyone on this computer |
| Create account | http://127.0.0.1:5000/register | Public registration, if enabled |
| User dashboard | http://127.0.0.1:5000/dashboard/ | Registered users |
| Admin dashboard | http://127.0.0.1:5000/admin/ | Administrators |

## Check a document

1. Register and sign in with a regular user account.
2. Select **Upload documents**, then choose or drop up to six PDF, DOCX, or TXT files. Give a single file a title or add a category if useful. To try the demo, upload `demo/rain-garden-draft.txt` after running `seed-demo`.
3. Open the document from **Documents** and select **Run similarity check**.
4. Review the overall score, highlighted text, and matched sources. Select **Download PDF** on the results page or in **Reports**. **Similarity history** preserves each completed check.
5. Open **Profile** to update your username or change your password.

## Run a batch

1. Select **Create a batch from these files** during upload, or open **Batch checking → Create a batch** and select up to 20 existing documents that you own.
2. Open the batch detail page to process the queued items automatically. **Process next document** also works without JavaScript. The status and counters update after each document. **Cancel remaining items** stops between documents and keeps completed results.
3. For unattended processing, run the batch worker in a second PowerShell window:

   ```powershell
   .\.venv\Scripts\python.exe -m flask --app run work-batches
   ```

   Add `--once` to exit when the queue is empty. Use Ctrl+C to stop the worker.

4. Open individual results or **Download batch report** for a combined PDF. One failed document does not stop subsequent items. An admin can requeue failed items.

## Admin guide

| Area | What to do |
| --- | --- |
| Dashboard and Analytics | Review totals, upload and check trends, score bands, processing status, common matching sources, and recent activity. |
| Users | Find an account by username, review registration details, or disable and re-enable a regular account. Create administrators from the CLI. |
| Documents | Filter records by owner, date, status, and category. Search titles or filenames, inspect extracted text, edit metadata, or delete documents. |
| Batch checking | Monitor jobs across accounts, advance pending work, cancel pending items, retry failed jobs, and download available batch summaries. |
| Source library | Add references using pasted text or a file. Include metadata as needed. Edit references, review match counts, or remove references from future checks. A URL is retained as a citation. |
| Similarity history and Reports | Review results across accounts. Search by document or owner, filter reports by date or minimum score, and download or remove report records. |
| Notifications and Announcements | Read account notifications, clear unread status, or send an announcement or system update to one username or all enabled accounts. |
| Feedback | Filter incoming submissions, set status and priority, and write a reply visible to the user. |
| Audit log | Filter by action, user, date, and status. Open entries for details or export the current results to CSV. |
| Model management | Enable local methods, set the sentence threshold and n-gram size, test configurations on two passages, and review saved configuration versions. |
| System settings | Set the per-file upload limit and enable or disable public registration. |

## Scoring and storage

TF-IDF cosine measures weighted word overlap, n-gram similarity measures consecutive-word overlap, sentence matching measures coverage above the configured threshold, and optional sequence matching compares token order. For each source, the enabled methods receive equal weight. The overall score is the highest source score. Each report records the active settings and counts the comparisons. Lower sentence thresholds may flag more passages. Template text and common phrasing can produce matches that still need review.

The application analyzes imported text and documents from the same batch. It does not crawl the web, use external AI services, run OCR, or work as a production internet deployment. Report PDFs use standard PDF fonts, which have limited support for scripts such as Malayalam. The browser continues to display the stored Unicode text.

Uploaded files are parsed in memory. Extracted text, metadata and historical analysis snapshots stay in SQLite. Deleting a document leaves existing analysis and report history, including the submitted text snapshot. Deleting an individual report removes its report record. Historical analysis and batch results remain accessible to the uploader and administrators.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `py` is unavailable | Install Python with the Windows launcher, reopen PowerShell, and run `py --version`. |
| `No module named flask` | Install the requirements using the same virtual-environment Python executable as the run command. |
| Port 5000 is busy | Start with `--port 5001` and replace 5000 in the page addresses. |
| PDF upload fails | Upload a readable, unencrypted PDF of at most 150 pages. Apply OCR separately to scanned pages and re-upload the text. |
| Upload exceeds a limit | Check the admin upload setting and keep extracted text under 40,000 characters. Split long documents into smaller files. |
| No sources or a 0% score | Import relevant references, run the demo seed, or compare related documents in one batch. Existing check records preserve their original results. |
| Batch is still pending | Open the batch page and leave it open, select **Process next document**, or run the CLI worker. |
| Session form expires | Refresh the page and try again. Keep `instance/session.key` stable between local runs. |

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite uses a fresh, in-memory database per test and covers registration, authentication, CSRF, roles, ownership, uploaded formats, similarity, batches, sources, PDF output, notifications, feedback, audit export, and model settings.

## Project layout

- `run.py` exposes the Flask application.
- `app/models.py` defines SQLite records and relationships.
- `app/views.py` contains authenticated routes.
- `app/services.py` extracts documents, scores similarity, and renders PDFs.
- `app/batches.py` advances queued work.
- `app/templates/` and `app/static/` provide the browser interface and bundled styles and charts.
- `tests/` holds integration tests and `demo/` supplies two example submissions.
