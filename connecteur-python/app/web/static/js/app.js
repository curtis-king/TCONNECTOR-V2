var _alertTimers = {};
function showToast(msg, type) {
  type = type || "ok";
  var scanFeed = (type === "err") && (msg.indexOf("Code-barres inconnu") === 0 || msg.indexOf("Erreur scan") === 0);
  if (window.NATIVE_ALERTS && !scanFeed) { alert(msg); return; }
  if (window.POS_DIALOG_ALERTS && type === "err" && !scanFeed) {
    showDialog(msg, "err");
    return;
  }
  var zone = document.getElementById("alert-zone");
  if (!zone) return;
  var key = type + "::" + msg;
  var prev = null;
  for (var i = 0; i < zone.children.length; i++) {
    if (zone.children[i].getAttribute("data-key") === key) { prev = zone.children[i]; break; }
  }
  if (prev) { clearTimeout(_alertTimers[key]); zone.removeChild(prev); }
  var card = document.createElement("div");
  card.className = "alert-card alert-" + type;
  card.setAttribute("data-key", key);
  var icon = "✔";
  if (type === "info") icon = "i";
  else if (type === "warn") icon = "!";
  else if (type === "err") icon = "✖";
  card.innerHTML = '<span class="alert-icon">' + icon + '</span><span class="alert-msg"></span><button class="alert-x" role="button" aria-label="Fermer">×</button>';
  card.querySelector(".alert-msg").textContent = msg;
  var close = card.querySelector(".alert-x");
  close.onclick = function() { dismissAlert(card, key); };
  zone.appendChild(card);
  var delay = type === "warn" ? 7000 : (type === "err" ? 12000 : 3800);
  _alertTimers[key] = setTimeout(function() { dismissAlert(card, key); }, delay);
  while (zone.children.length > 4) {
    var old = zone.children[0];
    dismissAlert(old, old.getAttribute("data-key"));
  }
}
function dismissAlert(card, key) {
  if (!card) return;
  if (_alertTimers[key]) clearTimeout(_alertTimers[key]);
  delete _alertTimers[key];
  card.classList.add("alert-out");
  setTimeout(function() { if (card.parentNode) card.parentNode.removeChild(card); }, 260);
}
function showDialog(msg, type, title) {
  type = type || "info";
  var root = document.getElementById("dialog-root");
  if (!root) { root = document.createElement("div"); root.id = "dialog-root"; document.body.appendChild(root); }
  var icons = {ok: "✔", info: "i", warn: "!", err: "✖"};
  var t = title || (type === "err" ? "Erreur" : type === "warn" ? "Attention" : type === "ok" ? "Succes" : "Information");
  root.innerHTML = '<div class="alert-overlay" id="dlg-overlay">'
    + '<div class="alert-dialog alert-' + type + '" role="dialog" aria-modal="true">'
    + '<div class="alert-dialog-head"><span class="alert-dialog-icon">' + (icons[type] || "i") + '</span><span class="alert-dialog-title"></span></div>'
    + '<div class="alert-dialog-msg"></div>'
    + '<div class="alert-dialog-actions"><button class="btn" id="dlg-ok">OK</button></div>'
    + '</div></div>';
  root.querySelector(".alert-dialog-title").textContent = t;
  root.querySelector(".alert-dialog-msg").textContent = msg;
  var overlay = root.querySelector("#dlg-overlay");
  var dialog = root.querySelector(".alert-dialog");
  function close() {
    if (document.getElementById("dlg-ok")) document.removeEventListener("keydown", onKey);
    overlay.classList.add("alert-out");
    dialog.classList.add("alert-out");
    setTimeout(function() { if (root.parentNode) root.parentNode.removeChild(root); }, 200);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", onKey);
  root.querySelector("#dlg-ok").onclick = close;
  overlay.addEventListener("click", function(e) { if (e.target === overlay) close(); });
  var ok = root.querySelector("#dlg-ok");
  if (ok) ok.focus();
}
function checkConnectivity() {
  fetch("/api/connectivity").then(function(r){return r.json()}).then(function(d){
    var dot = document.getElementById("net-dot");
    var lbl = document.getElementById("net-label");
    if (d.online) {
      dot.className = "net-dot on";
      lbl.textContent = "EN LIGNE";
    } else {
      dot.className = "net-dot off";
      lbl.textContent = "HORS LIGNE";
    }
  }).catch(function(){
    document.getElementById("net-dot").className = "net-dot off";
    document.getElementById("net-label").textContent = "HORS LIGNE";
  });
}
checkConnectivity();
setInterval(checkConnectivity, 15000);
function api(m,u,b){function call(t){var h={"Content-Type":"application/json"};if(t){window.CSRF_TOKEN=t;h["X-CSRF-Token"]=t}return fetch(u,{method:m,headers:h,body:b?JSON.stringify(b):undefined}).then(function(r){return r.json().then(function(d){if((r.status===400||r.status===403)&&d&&d.error&&d.error.indexOf("CSRF")>=0&&!t){return fetch("/api/csrf").then(function(r){return r.json()}).then(function(x){return call(x.token)})}return d})})}if(window.CSRF_TOKEN)return call(window.CSRF_TOKEN);return fetch("/api/csrf").then(function(r){return r.json()}).then(function(d){return call(d.token)})}
function syncNow(){showToast("Sync en cours...","info");api("POST","/api/sync").then(function(d){showToast(d.message||"OK","ok");setTimeout(function(){location.reload()},2000)})}
function switchTab(id){document.querySelectorAll(".tab-btn").forEach(function(b){b.classList.remove("active")});document.querySelectorAll(".tab-content").forEach(function(c){c.classList.remove("active")});document.getElementById("tab-btn-"+id).classList.add("active");document.getElementById("tab-"+id).classList.add("active")}
function saveForm(formId, url, next){var f=document.getElementById(formId);var d=new FormData(f);var obj={};d.forEach(function(v,k){obj[k]=v});api("POST",url,obj).then(function(r){if(r.ok){showToast("Sauvegarde OK","ok");if(next)next()}else{showToast("Erreur: "+(r.error||"inconnue"),"err")}}).catch(function(e){showToast("Erreur reseau: "+e,"err")})}
function toggleSidebar(){var s=document.querySelector(".sidebar");if(!s)return;var c=s.classList.toggle("collapsed");try{localStorage.setItem("tconn_sidebar",c?"1":"0")}catch(e){}}
(function(){try{if(localStorage.getItem("tconn_sidebar")==="1"){var s=document.querySelector(".sidebar");if(s)s.classList.add("collapsed")}}catch(e){}})();
