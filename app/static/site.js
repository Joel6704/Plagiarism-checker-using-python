document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.querySelector('#toggle');
  const sidebar = document.querySelector('#sidebar');
  if (toggle && sidebar) {
    const setSidebar = (visible) => {
      document.body.classList.toggle('nav-hidden', !visible);
      toggle.setAttribute('aria-expanded', String(visible));
    };
    if (matchMedia('(max-width: 800px)').matches) setSidebar(false);
    toggle.addEventListener('click', () => setSidebar(document.body.classList.contains('nav-hidden')));
  }

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const message = event.submitter?.dataset.confirm || form.dataset.confirm;
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      form.setAttribute('aria-busy', 'true');
      const submitter = event.submitter;
      if (submitter) {
        submitter.textContent = 'Working…';
        // Keep the clicked submit button's name and value in the request.
        setTimeout(() => { submitter.disabled = true; }, 0);
      }
    });
  });

  document.querySelectorAll('[data-drop]').forEach((zone) => {
    const input = zone.querySelector('input');
    const showFiles = () => {
      zone.querySelector('span').textContent = Array.from(input.files, (file) => file.name).join(', ') || 'Drop files here or browse';
    };
    for (const event of ['dragover', 'dragleave']) {
      zone.addEventListener(event, (e) => {
        e.preventDefault();
        zone.classList.toggle('drag-over', event === 'dragover');
      });
    }
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('drag-over');
      const files = e.dataTransfer.files;
      if (input.multiple || files.length === 1) input.files = files;
      showFiles();
    });
    input.addEventListener('change', showFiles);
  });

  const batch = document.querySelector('#advance');
  if (batch) {
    const timer = setTimeout(() => batch.requestSubmit(), 2400);
    document.addEventListener('submit', () => clearTimeout(timer), {once: true});
  }

  const chartData = document.querySelector('#chart-data');
  if (!chartData || !window.Chart) return;
  const data = JSON.parse(chartData.textContent);
  const options = {responsive: true, maintainAspectRatio: false};
  new Chart('activity', {type: 'line', options, data: {labels: data.labels.map((day) => day.slice(5)), datasets: [
    {label: 'Checks', data: data.checks, borderColor: '#315ee8', tension: .28},
    {label: 'Uploads', data: data.uploads, borderColor: '#118072', tension: .28}
  ]}});
  new Chart('distribution', {type: 'doughnut', options, data: {labels: ['Under 30%', '30–69%', '70% and up'], datasets: [
    {data: data.scores, backgroundColor: ['#315ee8', '#16a394', '#eaa247']}
  ]}});
  new Chart('processing', {type: 'bar', options: {...options, plugins: {legend: {display: false}}}, data: {
    labels: Object.keys(data.statuses), datasets: [{label: 'Documents', data: Object.values(data.statuses), backgroundColor: '#315ee8'}]
  }});
});
