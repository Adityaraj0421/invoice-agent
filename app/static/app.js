// Ledgerly mock "New Bill" form. No framework; everything is local.
const params = new URLSearchParams(location.search);
const runId = params.get("run") || null;
const mode = params.get("mode") || (runId ? "agent" : "manual");
const invoiceRef = params.get("invoice") || null;
let currency = null;
let firstInputAt = null;

const $ = (id) => document.getElementById(id);
const num = (s) => {
  const v = parseFloat(String(s || "").replace(/[^0-9.\-]/g, ""));
  return isNaN(v) ? 0 : v;
};

if (invoiceRef) {
  $("doc-img").src = `/invoices/${invoiceRef}.png`;
  $("doc-name").textContent = `${invoiceRef}.pdf`;
}

function addLine() {
  const n = $("lines").children.length + 1;
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input name="line${n}_description" aria-label="Line ${n} description" placeholder="Line ${n} description" autocomplete="off"></td>
    <td><input name="line${n}_quantity" aria-label="Line ${n} quantity" class="num qty" placeholder="0" autocomplete="off"></td>
    <td><input name="line${n}_unit_price" aria-label="Line ${n} unit price" class="num" placeholder="0.00" autocomplete="off"></td>
    <td class="num amt">0.00</td>`;
  $("lines").appendChild(tr);
  recalc();
}

function lineItems() {
  return [...$("lines").children].map((tr) => {
    const [d, q, u] = tr.querySelectorAll("input");
    return { description: d.value.trim(), quantity: q.value.trim(), unit_price: u.value.trim() };
  }).filter((l) => l.description || l.quantity || l.unit_price);
}

function recalc() {
  let sub = 0;
  for (const tr of $("lines").children) {
    const [, q, u] = tr.querySelectorAll("input");
    const amt = num(q.value) * num(u.value);
    tr.querySelector(".amt").textContent = amt.toFixed(2);
    sub += amt;
  }
  $("subtotal").textContent = sub.toFixed(2);
  const tax = num($("tax").value), total = num($("total").value);
  const r = $("reconcile");
  if (!$("total").value) { r.textContent = ""; r.className = "reconcile"; return; }
  const diff = Math.abs(sub + tax - total);
  if (diff < 0.01) { r.textContent = "✓ Lines + tax match total"; r.className = "reconcile ok"; }
  else { r.textContent = `⚠ Lines + tax = ${(sub + tax).toFixed(2)}, total differs by ${diff.toFixed(2)}`; r.className = "reconcile warn"; }
}

document.addEventListener("input", (e) => {
  if (!firstInputAt) firstInputAt = performance.now();
  if ($("status").classList.contains("saved")) markDraft();
  recalc();
});
// Enter never submits; only the Submit button does.
document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault(); });

$("currency").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-cur]");
  if (!b) return;
  if (!firstInputAt) firstInputAt = performance.now();
  currency = b.dataset.cur;
  for (const x of $("currency").children) x.classList.toggle("on", x === b);
});
$("add-line").addEventListener("click", addLine);

function markDraft() {
  $("status").textContent = "Draft"; $("status").className = "status draft";
  $("saved-banner").hidden = true;
}

$("submit").addEventListener("click", async () => {
  $("error").textContent = "";
  const payload = {
    run_id: runId, mode, invoice_ref: invoiceRef,
    vendor: $("vendor").value.trim(), invoice_number: $("invoice_number").value.trim(),
    invoice_date: $("invoice_date").value.trim(), due_date: $("due_date").value.trim(),
    currency, line_items: lineItems(), subtotal: $("subtotal").textContent,
    tax: $("tax").value.trim(), total: $("total").value.trim(),
    entry_seconds: firstInputAt ? +((performance.now() - firstInputAt) / 1000).toFixed(1) : null,
  };
  const missing = ["vendor", "invoice_number", "invoice_date", "total"].filter((k) => !payload[k]);
  if (missing.length) { $("error").textContent = `Missing: ${missing.join(", ")}`; return; }
  $("submit").disabled = true;
  try {
    const res = await fetch("/api/bills", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const rec = await res.json();
    $("status").textContent = "Saved"; $("status").className = "status saved";
    $("saved-detail").textContent = `${rec.bill_id} · ${payload.vendor} · ${payload.currency || ""} ${payload.total}`;
    $("saved-banner").hidden = false;
  } catch (err) {
    $("error").textContent = "Could not save: " + err;
  } finally { $("submit").disabled = false; }
});

addLine();
