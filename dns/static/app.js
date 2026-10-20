'use strict';

// Behaviour for the routes page. Everything that comes from containers is set as
// text, never as HTML, because container labels are not trusted.
(() => {
	const $ = (selector, root = document) => root.querySelector(selector);

	function el(tag, props = {}, ...children) {
		const node = document.createElement(tag);
		for (const [name, value] of Object.entries(props)) {
			if (name === 'class') node.className = value;
			else if (name === 'text') node.textContent = value;
			else node.setAttribute(name, value);
		}
		node.append(...children);
		return node;
	}

	async function copyText(text) {
		try {
			await navigator.clipboard.writeText(text);
			return true;
		} catch (error) {
			return false;
		}
	}

	// <button data-copy-from="some-id"> copies that element's text.
	for (const button of document.querySelectorAll('button[data-copy-from]')) {
		button.addEventListener('click', async () => {
			const ok = await copyText(document.getElementById(button.dataset.copyFrom).textContent);
			button.textContent = ok ? 'Copied' : 'Select the text and press Cmd+C';
		});
	}

	// Quote the hostname as a YAML string, and keep input as text even if it contains markup.
	const hostname = $('#site-hostname');
	const port = $('#site-port');
	const example = $('#add-site');
	if (hostname && port && example) {
		const template = example.textContent;
		const copyButton = $('[data-copy-from="add-site"]');
		const updateExample = () => {
			const valid = port.validity.valid;
			port.setAttribute('aria-invalid', String(!valid));
			copyButton.disabled = !valid;
			copyButton.textContent = 'Copy';
			if (!valid) return;
			const value = hostname.value.trim() || 'app.localhost';
			const portValue = port.value ? String(port.valueAsNumber) : '3000';
			example.textContent = template
				.replace('upstreams 3000', `upstreams ${portValue}`)
				.replace('app.localhost', () => JSON.stringify(value));
		};
		hostname.addEventListener('input', updateExample);
		port.addEventListener('input', updateExample);
		updateExample();
	}

	// The checks page opens each https site from this browser, which is the only way
	// to see the Mac's own DNS and whether it trusts the certificate.
	for (const row of document.querySelectorAll('tr[data-url]')) {
		const cell = $('.result', row);
		cell.textContent = 'Checking...';
		fetch(row.dataset.url, {mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(6000)})
			.then(() => { cell.textContent = 'Reachable'; })
			.catch(() => { cell.textContent = "Can't open from this browser"; });
	}

	const table = $('#routes');
	if (!table) return;

	// ---- status tooltips
	const tip = el('div', {id: 'tooltip', role: 'tooltip'});
	tip.hidden = true;
	document.body.append(tip);
	let anchor = null;
	let hideTimer = 0;

	function place(target) {
		const near = target.getBoundingClientRect();
		const box = tip.getBoundingClientRect();
		let top = near.bottom + 6;
		if (top + box.height > window.innerHeight - 8) top = Math.max(8, near.top - box.height - 6);
		const left = Math.min(Math.max(8, near.left), Math.max(8, window.innerWidth - box.width - 8));
		tip.style.top = `${top}px`;
		tip.style.left = `${left}px`;
	}

	function showTip(target) {
		clearTimeout(hideTimer);
		anchor = target;
		tip.replaceChildren();
		tip.append(el('div', {text: target.dataset.tip}));
		tip.hidden = false;
		place(target);
	}

	function hideTip(delay = 0) {
		clearTimeout(hideTimer);
		hideTimer = setTimeout(() => {
			tip.hidden = true;
			anchor = null;
		}, delay);
	}

	document.addEventListener('mouseover', (event) => {
		const target = event.target.closest('.tip');
		if (target) showTip(target);
	});
	document.addEventListener('mouseout', (event) => {
		if (event.target.closest('.tip')) hideTip(250);
	});
	document.addEventListener('focusin', (event) => {
		const target = event.target.closest('.tip');
		if (target) showTip(target);
	});
	document.addEventListener('focusout', (event) => {
		if (event.target.closest('.tip')) hideTip(150);
	});
	document.addEventListener('keydown', (event) => {
		if (event.key === 'Escape') hideTip();
	});
	tip.addEventListener('mouseenter', () => clearTimeout(hideTimer));
	tip.addEventListener('mouseleave', () => hideTip(150));
	table.addEventListener('click', async (event) => {
		const button = event.target.closest('button[data-copy-path]');
		if (!button) return;
		const ok = await copyText(button.dataset.copyPath);
		button.setAttribute('aria-label', ok ? 'Path copied' : 'Copy failed');
		button.title = ok ? 'Path copied' : 'Copy failed';
		$('#copy-status').textContent = ok ? 'Path copied' : "Couldn't copy. Select the path and press Cmd+C.";
	});

	// ---- the table: rows come from /api/rows, so it can update without a reload
	const body = $('tbody', table);
	const filterBox = $('#filter');
	const showOther = $('#show-other');
	const groupCompose = $('#group-compose');
	const hideHost = $('#hide-host');
	const count = $('#count');
	const noMatch = $('#no-match');
	const summary = $('#summary');
	const updated = $('#updated');
	const icons = {};
	for (const template of document.querySelectorAll('template[data-icon]')) {
		icons[template.dataset.icon] = template.content.firstElementChild;
	}

	const entries = new Map(); // key -> {tr, row}
	let rows = []; // latest data, in the server's order
	let firstPaint = true;
	let sort = {key: null, dir: 1};
	const rank = {fail: 0, ok: 1, none: 2};

	const strip = ({uptime, uptime_seconds, ...rest}) => rest; // what can change without rebuilding a row

	function cells(row) {
		const state = el('span', {class: 'tip', 'data-tip': row.state_label, tabindex: '0'}, icons[row.state].cloneNode(true));
		state.firstElementChild.setAttribute('aria-label', row.state_label);

		const name = el('td');
		if (row.link && /^https?:\/\//.test(row.link)) {
			name.append(el('a', {href: row.link, rel: 'noreferrer', text: row.name}));
		} else {
			name.append(row.name);
		}
		if (row.problem) name.append(el('br'), el('small', {text: row.problem}));

		const container = el('td', {text: row.container});

		const target = el('td');
		if (row.target) target.append(el('code', {text: row.target}));
		return [el('td', {}, state), name, container, el('td', {text: row.project}), target, el('td', {class: 'uptime', text: row.uptime})];
	}

	function fill(tr, row) {
		tr.replaceChildren(...cells(row));
	}

	function compare(a, b) {
		const key = sort.key;
		if (key === 'uptime') {
			const x = a.uptime_seconds;
			const y = b.uptime_seconds;
			if (x < 0 || y < 0) return (x < 0) - (y < 0); // unknown always last
			return (x - y) * sort.dir;
		}
		if (key === 'state') return (rank[a.state] - rank[b.state]) * sort.dir;
		return String(a[key]).localeCompare(String(b[key]), undefined, {numeric: true, sensitivity: 'base'}) * sort.dir;
	}

	const haystack = (row) => [row.name, row.container, row.project, row.compose_project, row.target, row.directory, row.state_label, row.problem, row.uptime].join('\n').toLowerCase();

	function arrange() {
		const words = filterBox.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
		let ordered = rows.map((row) => entries.get(row.key));
		if (sort.key) ordered = [...ordered].sort((a, b) => compare(a.row, b.row));
		let visible = 0;
		for (const entry of ordered) {
			const text = haystack(entry.row);
			const hidden = (!showOther.checked && entry.row.kind === 'other') || (hideHost.checked && entry.row.host) || !words.every((word) => text.includes(word));
			entry.tr.hidden = hidden;
			if (!hidden) visible += 1;
		}
		for (const header of body.querySelectorAll('.compose-group')) header.remove();
		if (groupCompose.checked) {
			const groups = new Map();
			for (const entry of ordered) {
				const row = entry.row;
				const key = JSON.stringify(row.compose_project ? [row.compose_project, row.directory] : ['', '']);
				if (!groups.has(key)) groups.set(key, []);
				groups.get(key).push(entry);
			}
			ordered = [];
			const keys = [...groups.keys()].sort((a, b) => {
				const [projectA, dirA] = JSON.parse(a);
				const [projectB, dirB] = JSON.parse(b);
				return Number(!projectA) - Number(!projectB) || projectA.localeCompare(projectB) || dirA.localeCompare(dirB);
			});
			for (const key of keys) {
				const members = groups.get(key);
				const shown = members.filter((entry) => !entry.tr.hidden);
				if (shown.length) {
					const [project, directory] = JSON.parse(key);
					const containers = new Set(shown.map(({row}) => row.container || row.name)).size;
					const info = el('span', {text: project || 'Standalone containers'}, el('small', {text: ` · ${containers} container(s)`}));
					const layout = el('div', {class: 'group-heading'}, info);
					const heading = el('th', {scope: 'rowgroup', colspan: '6'}, layout);
					if (project && directory) {
						const button = $('#copy-path-icon').content.firstElementChild.cloneNode(true);
						button.dataset.copyPath = directory;
						layout.append(el('span', {class: 'group-folder'}, el('small', {text: directory}), button));
					}
					ordered.push({tr: el('tr', {class: 'compose-group'}, heading)});
				}
				ordered.push(...members);
			}
		}
		const current = Array.from(body.children);
		if (ordered.some((entry, i) => current[i] !== entry.tr)) body.append(...ordered.map((entry) => entry.tr));

		count.textContent = visible === rows.length ? `${rows.length} shown` : `${visible} of ${rows.length} shown`;
		noMatch.hidden = visible !== 0 || rows.length === 0;
		for (const th of table.querySelectorAll('th[data-sort]')) {
			th.setAttribute('aria-sort', th.dataset.sort === sort.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none');
		}
	}

	function apply(data) {
		summary.textContent = data.summary;
		if (firstPaint) {
			body.replaceChildren(); // swap the server's first paint for ours
			firstPaint = false;
		}
		const seen = new Map();
		rows = data.rows.map((row) => {
			const base = [row.kind, row.container, row.name].join('|');
			const n = (seen.get(base) || 0) + 1;
			seen.set(base, n);
			return {...row, key: `${base}#${n}`};
		});
		const live = new Set();
		for (const row of rows) {
			live.add(row.key);
			const entry = entries.get(row.key);
			if (!entry) {
				const tr = el('tr');
				fill(tr, row);
				entries.set(row.key, {tr, row});
			} else {
				if (JSON.stringify(strip(entry.row)) !== JSON.stringify(strip(row))) fill(entry.tr, row);
				else if (entry.row.uptime !== row.uptime) $('.uptime', entry.tr).textContent = row.uptime;
				entry.row = row;
			}
		}
		for (const [key, entry] of entries) {
			if (!live.has(key)) {
				entry.tr.remove();
				entries.delete(key);
			}
		}
		arrange();
		if (anchor && !anchor.isConnected) hideTip();
	}

	table.querySelector('thead').addEventListener('click', (event) => {
		const th = event.target.closest('th[data-sort]');
		if (th) cycleSort(th.dataset.sort);
	});
	table.querySelector('thead').addEventListener('keydown', (event) => {
		const th = event.target.closest('th[data-sort]');
		if (th && (event.key === 'Enter' || event.key === ' ')) {
			event.preventDefault();
			cycleSort(th.dataset.sort);
		}
	});

	function cycleSort(key) {
		if (sort.key !== key) sort = {key, dir: 1};
		else if (sort.dir === 1) sort = {key, dir: -1};
		else sort = {key: null, dir: 1};
		arrange();
	}

	filterBox.addEventListener('input', arrange);
	showOther.addEventListener('change', arrange);
	groupCompose.addEventListener('change', arrange);
	hideHost.addEventListener('change', arrange);

	// ---- keeping it fresh
	let busy = false;
	async function refresh() {
		if (busy) return;
		busy = true;
		try {
			const response = await fetch('/api/rows', {cache: 'no-store', signal: AbortSignal.timeout(4000)});
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			apply(await response.json());
			updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
			updated.classList.remove('stale');
		} catch (error) {
			updated.textContent = "Can't reach this page right now. Trying again...";
			updated.classList.add('stale');
		} finally {
			busy = false;
		}
	}

	setInterval(() => {
		if (!document.hidden) refresh();
	}, 5000);
	document.addEventListener('visibilitychange', () => {
		if (!document.hidden) refresh();
	});
	refresh();
})();
