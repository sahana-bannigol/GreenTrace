/* GreenTrace – frontend utilities */

/**
 * API helper – POST/GET JSON, throws on error
 */
async function api(url, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== null) opts.body = JSON.stringify(body);

  const res = await fetch(url, opts);
  const data = await res.json();

  if (!data.success) {
    throw new Error(data.error || 'Unknown error');
  }
  return data;
}

/**
 * Toast notification
 */
let toastTimer;
function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
  }, 4500);
}

/**
 * Copy text to clipboard
 */
function copyText(text, label = 'Copied') {
  navigator.clipboard.writeText(text).then(() => toast(label + ' to clipboard', 'success'));
}

/**
 * Shorten an address/hash for display
 */
function shorten(str, start = 8, end = 6) {
  if (!str || str.length <= start + end + 3) return str;
  return str.slice(0, start) + '…' + str.slice(-end);
}

/**
 * Format a number with commas
 */
function formatNum(n) {
  return Number(n).toLocaleString();
}

/**
 * Relative time
 */
function relTime(isoStr) {
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/**
 * Auto-refresh live data on dashboard (every 30s)
 */
function startLiveRefresh() {
  if (!document.querySelector('.stats-row')) return;
  setInterval(async () => {
    try {
      const res = await api('/api/bonds');
      // Silently update outstanding bond counts
      (res.bonds || []).forEach(b => {
        const el = document.getElementById('outstanding-' + b.mpt_issuance_id);
        if (el) el.textContent = Math.floor(b.outstanding_bonds || 0);
      });
    } catch (e) { /* silent fail */ }
  }, 30000);
}

document.addEventListener('DOMContentLoaded', startLiveRefresh);
