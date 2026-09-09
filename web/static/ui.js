/* Shared page chrome for the control panel and the admin screen.
 *
 * toast() lived in both pages and the copies drifted: admin's grew an `err`
 * flag that renders through ui.css's #toast.err, index's never did — and four
 * of index's own call sites were already passing `true` into a parameter that
 * silently did not exist, so error toasts rendered in the success style. The
 * same rule as pin.js's header: two copies of a control is two chances to fix
 * only one of them. One copy now, next to the stylesheet that styles it.
 */
function toast(msg, err) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'show' + (err ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.className = '', 1800);
}
