import html
import logging
import functools
import hashlib
import json
import os
import threading
from datetime import timedelta
from flask import (
    Flask, render_template_string, request, jsonify,
    session, redirect, url_for
)
from config_manager import get_config, save_config
from sync_engine import (
    get_cache, get_metrics, sync_all, certify_single, sync_sfec_invoices,
    apply_config, get_retry_queue
)
from database import (
    ping_database, fetch_contacts, fetch_tax_rates,
    fetch_ledger_accounts, fetch_certified_invoices, list_all_tables,
    mark_to_monitor
)
from sfec_endpoints import check_health, preview_sfec_payload, validate_sfec_payload, certify_sqlite_invoice
from sfec_client import SfecClient
from connectivity import get_status, check_now
import sqlite_db
import invoice_engine
import pos_engine
import sage_writer
import sync_bidirectional
import pdf_generator

logger = logging.getLogger("t-connector.dashboard")

app = Flask(__name__)


def _esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


def _get_secret_key():
    cfg = get_config().get("dashboard", {})
    key = cfg.get("session_secret", "")
    if not key:
        machine_id = os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "default"))
        key = hashlib.sha256("t-connector-{}".format(machine_id).encode()).hexdigest()[:32]
    return key


app.secret_key = _get_secret_key()


def _login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        cfg = get_config()
        if not cfg.get("dashboard", {}).get("auth_enabled", True):
            return f(*args, **kwargs)
        if session.get("authenticated"):
            return f(*args, **kwargs)
        return redirect(url_for("login_page"))
    return decorated


CSS = """*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh;display:flex}
.sidebar{width:250px;background:#ffffff;border-right:1px solid #e2e8f0;padding:0;position:fixed;top:0;left:0;bottom:0;overflow-y:auto;z-index:100;box-shadow:2px 0 16px rgba(15,23,42,0.05);display:flex;flex-direction:column;transition:width 0.25s ease}
.sidebar.collapsed{width:76px}
.sidebar-top{display:flex;align-items:center;justify-content:space-between;padding:18px 16px 14px;border-bottom:1px solid #eef2f7}
.sidebar-logo{display:flex;align-items:center;gap:10px;text-decoration:none}
.sidebar-logo .icon{width:34px;height:34px;flex-shrink:0;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#6366f1);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;box-shadow:0 4px 10px rgba(59,130,246,0.3)}
.sidebar-logo span{font-size:15px;font-weight:700;color:#1e293b;letter-spacing:-0.3px;white-space:nowrap;transition:opacity 0.2s}
.sidebar-toggle{width:32px;height:32px;flex-shrink:0;border:none;border-radius:8px;background:#f1f5f9;color:#64748b;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all 0.18s}
.sidebar-toggle:hover{background:#e2e8f0;color:#1e293b}
.sidebar-toggle svg{width:18px;height:18px}
.sidebar-nav{list-style:none;padding:8px 0;margin:0;flex:1;overflow-y:auto}
.sidebar.collapsed .sidebar-logo span,
.sidebar.collapsed .sidebar-nav .nav-label,
.sidebar.collapsed .sidebar-nav li a span:not(.icon),
.sidebar.collapsed .sidebar-foot .net-indicator span:not(.net-dot){display:none}
.sidebar.collapsed .sidebar-nav li a{justify-content:center;padding:11px 0}
.sidebar.collapsed .sidebar-nav li a .icon{margin:0}
.sidebar.collapsed .sidebar-toggle svg{transform:rotate(180deg)}
.sidebar-foot{position:sticky;bottom:0;background:#ffffff;padding:14px 20px;border-top:1px solid #eef2f7}
.net-indicator{display:flex;align-items:center;gap:8px;font-size:12px;color:#64748b}
.net-dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.net-dot.on{background:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,0.2)}
.net-dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,0.2)}
.net-dot.unknown{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,0.2)}
.sidebar-nav{list-style:none;padding:0;margin:0}
.sidebar-nav li{margin:2px 8px 2px 10px}
.sidebar-nav li a{display:flex;align-items:center;gap:12px;padding:10px 14px;color:#64748b;text-decoration:none;font-size:13px;font-weight:500;transition:all 0.18s;border-radius:10px;border-left:3px solid transparent}
.sidebar-nav li a:hover{background:#f1f5f9;color:#1e293b}
.sidebar-nav li a.active{background:linear-gradient(90deg,rgba(59,130,246,0.12),rgba(99,102,241,0.06));color:#3b82f6;border-left-color:#3b82f6;font-weight:600;box-shadow:inset 0 0 0 1px rgba(59,130,246,0.08)}
.sidebar-nav .icon{width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:15px;opacity:0.75;transition:all 0.18s}
.sidebar-nav .icon svg{width:18px;height:18px}
.sidebar-nav li a:hover .icon,.sidebar-nav li a.active .icon{opacity:1}
.sidebar-nav .nav-label{font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#94a3b8;padding:16px 22px 6px}
.sidebar-nav li a .logout{color:#dc2626}
.sidebar-nav li a.logout-link{color:#dc2626}
.sidebar-nav li a.logout-link:hover{background:#fef2f2;color:#b91c1c}
.main-content{margin-left:250px;flex:1;padding:24px 32px;max-width:calc(100vw - 250px);transition:margin-left 0.25s ease}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #eef2f7}
.topbar-title{font-size:21px;font-weight:700;color:#0f172a}
.topbar-actions{display:flex;align-items:center;gap:10px}
.user-chip{display:inline-flex;align-items:center;gap:8px;background:#fff;border:1px solid #e2e8f0;border-radius:999px;padding:6px 14px;font-size:12px;font-weight:600;color:#334155}
.user-chip .ava{width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#3b82f6,#6366f1);color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
.page-header{margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.page-header h1{font-size:22px;font-weight:700;color:#1e293b;margin:0}
.breadcrumb{font-size:12px;color:#64748b;margin-bottom:4px}
.breadcrumb a{color:#3b82f6;text-decoration:none;font-weight:500}
.breadcrumb a:hover{text-decoration:underline}
.card{background:#fff;border-radius:14px;border:1px solid #e2e8f0;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(15,23,42,0.04)}
.card h2{font-size:15px;font-weight:600;color:#1e293b;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.card h2::before{content:'';display:inline-block;width:4px;height:18px;background:linear-gradient(180deg,#3b82f6,#6366f1);border-radius:2px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px}
.stat{background:#fff;border-radius:12px;padding:18px;border:1px solid #e2e8f0;transition:all 0.2s;position:relative;overflow:hidden}
.stat::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#3b82f6,#6366f1);opacity:0;transition:opacity 0.2s}
.stat:hover{box-shadow:0 8px 20px rgba(15,23,42,0.07);transform:translateY(-3px);border-color:#cbd5e1}
.stat:hover::after{opacity:1}
.stat .value{font-size:26px;font-weight:700;color:#0f172a;margin-bottom:4px;letter-spacing:-0.5px}
.stat .label{font-size:12px;color:#64748b;font-weight:500}
.stat .icon-chip{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;margin-bottom:12px}
.stat .icon-chip svg{width:19px;height:19px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600}
.badge-ok{background:#dcfce7;color:#166534}
.badge-err{background:#fee2e2;color:#991b1b}
.badge-warn{background:#fef3c7;color:#92400e}
.badge-info{background:#dbeafe;color:#1e40af}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#f8fafc;color:#64748b;text-align:left;padding:10px 12px;border-bottom:2px solid #e2e8f0;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600}
td{padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#334155}
tr:hover{background:#f8fafc}
label{display:block;color:#475569;font-size:11px;font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
input[type=text],input[type=password],input[type=number],input[type=email],input[type=date],select,textarea{
  background:#fff;border:1.5px solid #e2e8f0;color:#1e293b;
  padding:9px 12px;border-radius:8px;width:100%;font-size:13px;
  font-family:inherit;transition:all 0.2s;
}
input:focus,select:focus,textarea:focus{
  outline:none;border-color:#3b82f6;
  box-shadow:0 0 0 3px rgba(59,130,246,0.1);
  background:#fff;
}
input::placeholder{color:#94a3b8}
select{cursor:pointer;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 12px center;padding-right:36px}
textarea{resize:vertical;min-height:60px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:all 0.15s;text-decoration:none}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.btn:active{transform:translateY(0)}
.btn-primary{background:linear-gradient(135deg,#3b82f6,#6366f1);color:#fff;box-shadow:0 2px 8px rgba(59,130,246,0.25)}
.btn-primary:hover{box-shadow:0 4px 16px rgba(59,130,246,0.35)}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 2px 8px rgba(16,185,129,0.25)}
.btn-danger{background:#ef4444;color:#fff}
.btn-warning{background:#f59e0b;color:#fff}
.btn-sm{padding:5px 12px;font-size:11px;border-radius:7px}
.btn-ghost{background:transparent;color:#64748b;border:1px solid #e2e8f0}
.btn-ghost:hover{background:#f1f5f9;color:#1e293b;border-color:#cbd5e1;box-shadow:none}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:768px){.grid-2{grid-template-columns:1fr}}
@media print{.no-print{display:none!important}body{background:#fff;color:#000}
.tab-btn:hover{color:#3b82f6;background:#f1f5f9}
.tab-btn.active{background:#fff;color:#3b82f6;border-color:#3b82f6;border-bottom:1px solid #fff}
.tab-content{display:none;background:#fff;border:1px solid #e2e8f0;border-radius:0 8px 8px 8px;padding:20px;margin-bottom:16px}
.tab-content.active{display:block}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;font-size:13px;font-weight:600;z-index:9999;opacity:0;transition:opacity 0.3s;box-shadow:0 4px 12px rgba(0,0,0,0.15)}
.toast.show{opacity:1}
.toast-ok{background:#dcfce7;color:#166534;border:1px solid #86efac}
.toast-err{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.toast-warn{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}
.net-indicator{display:flex;align-items:center;gap:6px;font-size:12px;color:#64748b}
.form-row{margin-bottom:8px}
.desc{font-size:11px;color:#64748b;margin-top:2px}
.queue-badge{background:#ede9fe;color:#5b21b6;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.invoice-layout{display:grid;grid-template-columns:1fr 360px;gap:20px;align-items:start}
@media(max-width:1024px){.invoice-layout{grid-template-columns:1fr}}
.invoice-main{min-width:0}
.invoice-sidebar{position:sticky;top:24px}
.section-title{font-size:13px;font-weight:700;color:#3b82f6;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';display:inline-block;width:4px;height:16px;background:linear-gradient(180deg,#3b82f6,#6366f1);border-radius:2px}
.invoice-meta{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:768px){.invoice-meta{grid-template-columns:1fr 1fr}}
.invoice-meta .full-width{grid-column:1/-1}
.line-item{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-bottom:10px;transition:all 0.2s;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.line-item:hover{border-color:#cbd5e1;box-shadow:0 4px 12px rgba(0,0,0,0.08)}
.line-item-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.line-item-title{font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px}
.line-item-grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:10px;align-items:end}
@media(max-width:768px){.line-item-grid{grid-template-columns:1fr 1fr;gap:8px}.line-item-grid .full-width-mobile{grid-column:1/-1}}
.line-item-actions{display:flex;gap:6px;align-items:center}
.totals-panel{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;position:sticky;top:24px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.totals-panel h3{color:#1e293b;font-size:14px;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #e2e8f0}
.total-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;font-size:13px;color:#64748b}
.total-row.grand-total{border-top:2px solid #e2e8f0;margin-top:8px;padding-top:12px;font-size:18px;font-weight:700;color:#1e293b}
.total-row .total-label{font-weight:600;text-transform:uppercase;letter-spacing:0.5px;font-size:11px}
.total-row .total-value{font-weight:700;color:#1e293b}
.form-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px;padding-top:16px;border-top:1px solid #e2e8f0}
.form-actions .btn{flex:1;min-width:140px;justify-content:center;display:inline-flex;align-items:center;justify-content:center;padding:12px 20px;font-size:14px}
@media(max-width:768px){.form-actions .btn{min-width:100%}}
.page-header{margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.page-header h1{font-size:22px;font-weight:700;color:#1e293b;margin:0}
.breadcrumb{font-size:12px;color:#64748b;margin-bottom:4px}
.breadcrumb a{color:#3b82f6;text-decoration:none;font-weight:500}
.breadcrumb a:hover{text-decoration:underline}
.client-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.client-card .contact-select-wrapper{position:relative}
.client-card .contact-select-wrapper::after{content:'▼';position:absolute;right:12px;bottom:12px;font-size:10px;color:#64748b;pointer-events:none}
.invoice-header-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
.invoice-header-card .grid-2{gap:12px}
.invoice-header-card label{font-size:10px;letter-spacing:0.8px;color:#64748b}
.invoice-header-card input,.invoice-header-card select{padding:8px 12px;font-size:12px;border:1.5px solid #e2e8f0;background:#fff}
.status-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px}
.status-brouillon{background:#f1f5f9;color:#475569}
.status-valide{background:#dcfce7;color:#166534}
.status-a_comptabiliser{background:#dbeafe;color:#1e40af}
.line-empty{text-align:center;padding:40px 20px;color:#94a3b8;font-size:13px}
.line-empty .icon{font-size:40px;margin-bottom:10px;opacity:0.4}
.product-search-wrapper{position:relative}
.product-search-results{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin-top:4px;max-height:200px;overflow-y:auto;z-index:50;box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.product-search-item{padding:10px 12px;cursor:pointer;border-bottom:1px solid #f1f5f9;font-size:13px}
.product-search-item:hover{background:#f8fafc}
.product-search-item .ref{color:#3b82f6;font-weight:600;font-size:11px}
.product-search-item .price{color:#10b981;font-weight:600;font-size:12px;margin-left:auto}"""

SIDEBAR_ICONS = {
    "dashboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg>',
    "invoice": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
    "card": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',
    "cart": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>',
    "users": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    "user": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    "hourglass": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "logout": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
}

SIDEBAR_ITEMS = [
    ("/", "dashboard", "Dashboard"),
    ("/invoices", "invoice", "Factures Sage"),
    ("/billing", "card", "Facturation"),
    ("/pos", "cart", "Vente POS"),
    ("/clients", "users", "Clients"),
    ("/sales", "clock", "Historique"),
    ("/vendeurs", "user", "Vendeurs"),
    ("/pending", "hourglass", "En attente"),
    ("/certified", "check", "Certifiees"),
    ("/config", "settings", "Config"),
]


def _sidebar(path="../.."):
    def _item(url, icon, label):
        if url == "/":
            active = "active" if path == "/" else ""
        else:
            active = "active" if path.startswith(url) else ""
        return '<li><a href="{url}" class="{active}"><span class="icon">{icon}</span> {label}</a></li>'.format(
            url=url, active=active, icon=SIDEBAR_ICONS[icon], label=label
        )

    groups = [
        ("Principal", SIDEBAR_ITEMS[:4]),
        ("Gestion", SIDEBAR_ITEMS[4:]),
    ]
    nav_parts = []
    for gi, (group_name, items) in enumerate(groups):
        if gi > 0:
            nav_parts.append('<li class="nav-label">' + group_name + "</li>")
        else:
            nav_parts.append('<li class="nav-label">' + group_name + "</li>")
        nav_parts.extend(_item(*item) for item in items)
    nav = "".join(nav_parts)
    nav += ('<li><a href="/logout" class="logout-link"><span class="icon">{icon}</span> Deconnexion</a></li>').format(
        icon=SIDEBAR_ICONS["logout"]
    )

    return """<div class="sidebar">
  <div class="sidebar-top">
    <a href="/" class="sidebar-logo">
      <div class="icon">TC</div>
      <span>T-CONNECTOR</span>
    </a>
    <button class="sidebar-toggle no-print" onclick="toggleSidebar()" aria-label="Replier le menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
    </button>
  </div>
  <ul class="sidebar-nav">""" + nav + """</ul>
  <div class="sidebar-foot">
    <div class="net-indicator">
      <span class="net-dot unknown" id="net-dot"></span>
      <span id="net-label" style="font-size:11px;font-weight:600">...</span>
    </div>
  </div>
</div>
"""

TOAST = """<div class="toast" id="toast"></div>
<script>
function showToast(msg, type) {
  var t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show toast-" + (type || "ok");
  setTimeout(function(){ t.className = "toast"; }, 3500);
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
</script>"""

FOOTER = """</div>
""" + TOAST + """
<script>
function api(m,u,b){return fetch(u,{method:m,headers:{"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined}).then(function(r){return r.json()})}
function syncNow(){showToast("Sync en cours...","info");api("POST","/api/sync").then(function(d){showToast(d.message||"OK","ok");setTimeout(function(){location.reload()},2000)})}
function switchTab(id){document.querySelectorAll(".tab-btn").forEach(function(b){b.classList.remove("active")});document.querySelectorAll(".tab-content").forEach(function(c){c.classList.remove("active")});document.getElementById("tab-btn-"+id).classList.add("active");document.getElementById("tab-"+id).classList.add("active")}
function saveForm(formId, url, next){var f=document.getElementById(formId);var d=new FormData(f);var obj={};d.forEach(function(v,k){obj[k]=v});api("POST",url,obj).then(function(r){if(r.ok){showToast("Sauvegarde OK","ok");if(next)next()}else{showToast("Erreur: "+(r.error||"inconnue"),"err")}}).catch(function(e){showToast("Erreur reseau: "+e,"err")})}
function toggleSidebar(){var s=document.querySelector(".sidebar");if(!s)return;var c=s.classList.toggle("collapsed");try{localStorage.setItem("tconn_sidebar",c?"1":"0")}catch(e){}}
(function(){try{if(localStorage.getItem("tconn_sidebar")==="1"){var s=document.querySelector(".sidebar");if(s)s.classList.add("collapsed")}}catch(e){}})();
</script></body></html>"""


def _current_page():
    p = request.path
    if p == "/":
        return "Dashboard", "Tableau de bord"
    for url, _ic, label in SIDEBAR_ITEMS:
        if p.startswith(url) and url != "/":
            return url.strip("/") or "page", label
    return "", p.strip("/")


def _page(body):
    page_key, page_label = _current_page()
    email = session.get("login_email", "")
    initial = (email[:1].upper() if email else "U")
    topbar = ('<div class="topbar"><div class="topbar-title">{title}</div>'
              '<div class="topbar-actions">'
              '{sync}'
              '<span class="user-chip"><span class="ava">{initial}</span>{email}</span>'
              '<a class="btn btn-sm btn-ghost no-print" href="/logout">Deconnexion</a>'
              '</div></div>').format(
        title=_esc(page_label),
        sync=('<button class="btn btn-sm btn-primary no-print" onclick="syncNow()">Synchroniser</button>' if page_key != "config" else ""),
        initial=_esc(initial), email=_esc(email)
    )
    return "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>" \
           "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
           "<title>T-CONNECTOR &middot; {title}</title><style>".format(title=_esc(page_label)) \
           + CSS + "</style></head><body>" \
           + _sidebar(request.path) + '<div class="main-content">' + topbar + body + FOOTER


LOGIN_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>T-CONNECTOR - Connexion</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  min-height:100vh;display:flex;background:#f1f5f9;color:#1e293b;
}
.auth-sidebar{
  width:260px;background:#ffffff;border-right:1px solid #e2e8f0;
  position:fixed;top:0;left:0;bottom:0;overflow-y:auto;z-index:100;
  display:flex;flex-direction:column;
  box-shadow:2px 0 8px rgba(0,0,0,0.04);
}
.auth-logo{display:flex;align-items:center;gap:10px;padding:20px;border-bottom:1px solid #e2e8f0}
.auth-logo .ico{
  width:34px;height:34px;border-radius:9px;
  background:linear-gradient(135deg,#3b82f6,#6366f1);
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;
}
.auth-logo span{font-size:15px;font-weight:700;color:#1e293b;letter-spacing:-0.3px}
.auth-menu{list-style:none;padding:12px 0;flex:1}
.auth-menu li{
  display:flex;align-items:center;gap:12px;padding:10px 20px;
  color:#64748b;font-size:13px;font-weight:500;
}
.auth-menu li .icon{width:20px;height:20px;display:flex;align-items:center;justify-content:center;opacity:0.7}
.auth-menu li .icon svg{width:18px;height:18px}
.auth-foot{padding:16px 20px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;line-height:1.6}
.auth-main{margin-left:260px;flex:1;display:flex;align-items:center;justify-content:center;padding:40px 24px;min-height:100vh}
.card{
  width:420px;max-width:100%;
  background:#fff;border:1px solid #e2e8f0;border-radius:16px;
  padding:40px 36px;box-shadow:0 10px 30px rgba(15,23,42,0.06);
}
.logo{
  width:56px;height:56px;border-radius:14px;margin:0 auto 16px;
  background:linear-gradient(135deg,#3b82f6,#6366f1);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 6px 18px rgba(59,130,246,0.25);
}
.logo svg{width:30px;height:30px}
h1{
  text-align:center;font-weight:700;color:#1e293b;font-size:22px;
  margin-bottom:4px;letter-spacing:-0.5px;
}
.sub{text-align:center;font-size:13px;color:#64748b;margin-bottom:28px}
.field{margin-bottom:18px}
.field label{
  display:block;font-size:11px;font-weight:600;color:#475569;
  margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;
}
.field input{
  width:100%;padding:12px 14px;font-size:14px;
  background:#fff;color:#1e293b;
  border:1.5px solid #e2e8f0;border-radius:10px;
  font-family:inherit;transition:all 0.2s;
}
.field input:focus{
  outline:none;border-color:#3b82f6;
  box-shadow:0 0 0 4px rgba(59,130,246,0.1);
}
.field input::placeholder{color:#94a3b8}
.btn{
  width:100%;padding:13px;margin-top:6px;
  background:linear-gradient(135deg,#3b82f6,#6366f1);
  color:#fff;border:none;border-radius:10px;
  font-size:15px;font-weight:700;cursor:pointer;
  font-family:inherit;letter-spacing:0.3px;
  box-shadow:0 4px 16px rgba(59,130,246,0.25);
  transition:all 0.2s;display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn:hover{
  box-shadow:0 8px 24px rgba(59,130,246,0.35);
  transform:translateY(-1px);
}
.btn:active{transform:translateY(0)}
.btn svg{width:16px;height:16px}
.error{
  background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;
  font-size:13px;text-align:center;
  padding:12px;border-radius:10px;margin-bottom:18px;
}
.features{
  display:flex;justify-content:center;gap:16px;
  margin-top:28px;padding-top:20px;
  border-top:1px solid #e2e8f0;
}
.feat{text-align:center;flex:1}
.feat .ico{
  width:34px;height:34px;border-radius:9px;margin:0 auto 6px;
  display:flex;align-items:center;justify-content:center;
  background:#eff6ff;border:1px solid #dbeafe;
}
.feat .ico svg{width:17px;height:17px}
.feat span{font-size:10px;color:#64748b;display:block;line-height:1.3}
.foot{text-align:center;margin-top:24px;font-size:11px;color:#94a3b8}
@media(max-width:768px){.auth-sidebar{display:none}.auth-main{margin-left:0;padding:24px 16px}}
</style></head><body>
<div class="auth-sidebar">
  <div class="auth-logo">
    <div class="ico">TC</div>
    <span>T-CONNECTOR</span>
  </div>
  <ul class="auth-menu">
    <li><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg></span> Tableau de bord</li>
    <li><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg></span> Factures Sage</li>
    <li><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></span> Certification SFEC</li>
    <li><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></span> Synchronisation</li>
    <li><span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg></span> Vente POS</li>
  </ul>
  <div class="auth-foot">Connecteur Sage 100<br>Certification SFEC &middot; &copy; 2025 Sodico</div>
</div>

<div class="auth-main">
<div class="card">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
    </svg>
  </div>
  <h1>T-CONNECTOR</h1>
  <p class="sub">Connecteur Sage 100 &middot; Certification SFEC</p>

  {error}

  <form method="POST" action="/login">
    <div class="field">
      <label>Adresse email</label>
      <input type="email" name="email" placeholder="admin@example.com" required autofocus>
    </div>
    <div class="field">
      <label>Mot de passe</label>
      <input type="password" name="password" placeholder="Entrez votre mot de passe" required>
    </div>
    <button type="submit" class="btn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
      Se connecter
    </button>
  </form>

  <div class="features">
    <div class="feat">
      <div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>
      <span>Facturation<br>integree</span>
    </div>
    <div class="feat">
      <div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
      <span>Certification<br>SFEC</span>
    </div>
    <div class="feat">
      <div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="#6366f1" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg></div>
      <span>Synchronisation<br>Sage 100</span>
    </div>
    <div class="feat">
      <div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg></div>
      <span>Module<br>vente POS</span>
    </div>
  </div>
  <p class="foot">T-CONNECTOR SFEC &copy; 2025 &mdash; Sodico</p>
</div>
</div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if session.get("authenticated"):
            return redirect("/")
        return render_template_string(LOGIN_HTML.replace("{error}", ""))
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    cfg = get_config().get("dashboard", {})
    if email == cfg.get("login_email") and password == cfg.get("login_password"):
        session["authenticated"] = True
        session["login_email"] = email
        session.permanent = True
        app.permanent_session_lifetime = timedelta(
            hours=cfg.get("session_max_age_hours", 24)
        )

        try:
            import sqlite_db
            with sqlite_db.get_cursor() as cur:
                cur.execute("SELECT id FROM vendeurs WHERE email = ? AND est_actif = 1", (email,))
                row = cur.fetchone()
                if row:
                    session["vendeur_id"] = row["id"]
                else:
                    code = "VEN-{:04d}".format(1)
                    cur.execute("SELECT COUNT(*) as c FROM vendeurs")
                    cnt = cur.fetchone()["c"]
                    code = "VEN-{:04d}".format(cnt + 1)
                    cur.execute(
                        "INSERT INTO vendeurs (code, nom, prenom, email, role) VALUES (?, ?, ?, ?, ?)",
                        (code, "Admin", "", email, "responsable")
                    )
                    session["vendeur_id"] = cur.lastrowid
        except Exception:
            pass

        return redirect("/")
    return render_template_string(
        LOGIN_HTML.replace("{error}", '<div class="error">Identifiants incorrects</div>')
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@_login_required
def index():
    m = get_metrics()
    sfec = check_health()
    conn = get_status()
    retry_q = get_retry_queue()

    cache = get_cache()
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)

    db_ok = "badge-ok" if m.get("db_connected") else "badge-err"
    db_txt = "Connectee" if m.get("db_connected") else "Deconnectee"
    sfec_ok = "badge-ok" if sfec.get("connected") else "badge-err"
    sfec_txt = "Connectee" if sfec.get("connected") else "Deconnectee"
    net_cls = "badge-ok" if conn.get("online") else "badge-err"
    net_txt = "EN LIGNE" if conn.get("online") else "HORS LIGNE"

    retry_html = ""
    if retry_q:
        retry_html = '<tr><td>File d\'attente SFEC</td><td><span class="queue-badge">{} factures en attente</span></td></tr>'.format(len(retry_q))

    body = """
<div class="card"><h2>Tableau de bord</h2><div class="stat-grid">
<div class="stat"><div class="icon-chip" style="background:#eff6ff;color:#3b82f6"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div><div class="value">{sales}</div><div class="label">Factures Vente</div></div>
<div class="stat"><div class="icon-chip" style="background:#fef3c7;color:#d97706"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/></svg></div><div class="value">{purchases}</div><div class="label">Factures Achat</div></div>
<div class="stat"><div class="icon-chip" style="background:#ede9fe;color:#7c3aed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div><div class="value">{val_count}</div><div class="label">A comptabiliser</div></div>
<div class="stat"><div class="icon-chip" style="background:#dcfce7;color:#16a34a"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></div><div class="value">{contacts}</div><div class="label">Contacts</div></div>
<div class="stat"><div class="icon-chip" style="background:#dbeafe;color:#2563eb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></div><div class="value">{tax}</div><div class="label">Taux TVA</div></div>
<div class="stat"><div class="icon-chip" style="background:#fce7f3;color:#db2777"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg></div><div class="value">{syncs}</div><div class="label">Syncs</div></div>
<div class="stat"><div class="icon-chip" style="background:#ecfdf5;color:#059669"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div><div class="value">{certs}</div><div class="label">Certifiees SFEC</div></div>
</div></div>
<div class="grid-2"><div class="card"><h2>Etat du systeme</h2><table>
<tr><td>Base de donnees</td><td><span class="badge {db_ok}">{db_txt}</span></td></tr>
<tr><td>Connexion Internet</td><td><span class="badge {net_cls}">{net_txt}</span></td></tr>
<tr><td>Derniere sync</td><td>{last_sync}</td></tr>
<tr><td>Erreur</td><td>{error}</td></tr>
<tr><td>SFEC API</td><td><span class="badge {sfec_ok}">{sfec_txt}</span></td></tr>
<tr><td>URL SFEC</td><td>{sfec_url}</td></tr>
{retry_html}
<tr><td>Polling auto</td><td><span class="badge badge-ok">Actif</span></td></tr>
</table></div>
<div class="card no-print"><h2>Actions</h2>
<button class="btn btn-primary" onclick="syncNow()">Sync Manuelle</button>
<button class="btn btn-warning" onclick="location.href='/pending'" style="margin-left:8px">A certifier</button>
<button class="btn btn-success" onclick="location.href='/certified'" style="margin-left:8px">Certifiees</button>
<div style="margin-top:8px;font-size:12px;color:#64748b" id="sync-status"></div>
</div></div>""".format(
        sales=m.get("sales_invoices", 0), purchases=m.get("purchase_invoices", 0),
        val_count=val_count,
        contacts=m.get("contacts", 0), tax=m.get("tax_rates", 0),
        syncs=m.get("sync_count", 0), certs=m.get("certified_count", 0),
        db_ok=db_ok, db_txt=db_txt, last_sync=_esc(m.get("last_sync_at") or "Jamais"),
        error=_esc(m.get("last_error") or "Aucune"),
        sfec_ok=sfec_ok, sfec_txt=sfec_txt, sfec_url=_esc(sfec.get("url", "")),
        net_cls=net_cls, net_txt=net_txt, retry_html=retry_html
    )
    return _page(body)


@app.route("/invoices")
@_login_required
def invoices_page():
    inv_type = request.args.get("type", "sale")
    cache = get_cache()
    invoices = cache.get("purchase_invoices" if inv_type == "purchase" else "sales_invoices", [])
    rows = ""
    for inv in invoices[:200]:
        s = inv.get("statut", "")
        sb = "badge-ok" if s == "COMPTABILISE" else "badge-warn" if s in ("CONFIRME", "A COMPTABILISER") else ""
        ss = inv.get("sfec_statut", "")
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-warn" if ss == "EN_COURS" else "badge-err" if ss == "ERREUR" else "badge-info" if ss == "A_SURVEILLER" else ""
        sftxt = "Certifie" if ss == "CERTIFIE" else "Deja certifie" if ss == "DEJA_CERTIFIE" else "En cours" if ss == "EN_COURS" else "Erreur" if ss == "ERREUR" else "A surveiller" if ss == "A_SURVEILLER" else "A certifier" if inv_type == "sale" else "-"
        cert_num = inv.get("sfec_num_certif", "") or ""
        val = inv.get("valide", 0)
        val_cls = "badge-ok" if val == 1 else "badge-err"
        val_txt = "Valide" if val == 1 else "Non valide"
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td>{}</td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), sb, _esc(s), val_cls, val_txt, sfb, _esc(sftxt), _esc(cert_num or "-")
        )
    sale_cls = "btn-primary" if inv_type == "sale" else ""
    purchase_cls = "btn-primary" if inv_type == "purchase" else ""
    empty = '<tr><td colspan="8" style="text-align:center;color:#64748b">Aucune facture</td></tr>' if not rows else ""
    body = """
<div class="card"><h2>Factures ({inv_type})</h2>
<div style="margin-bottom:12px" class="no-print">
<a href="/invoices?type=sale" class="btn btn-sm {sale_cls}">Vente</a>
<a href="/invoices?type=purchase" class="btn btn-sm {purchase_cls}">Achat</a>
<button class="btn btn-sm btn-primary" onclick="syncNow()" style="margin-left:8px">Sync</button>
</div>
<table><thead><tr><th>Numero</th><th>Date</th><th>Tiers</th>
<th style="text-align:right">Montant TTC</th><th>Statut</th><th>Valide</th><th>SFEC</th><th>N Certif</th></tr></thead>
<tbody>{rows}{empty}</tbody></table></div>""".format(
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls,
        rows=rows, empty=empty
    )
    return _page(body)


@app.route("/pending")
@_login_required
def pending_page():
    cache = get_cache()
    sfec_cfg = get_config().get("sfec", {})
    sfec_ids = set()
    try:
        client = SfecClient()
        sfec_result = client.list_invoices(page=1, page_size=500)
        for inv in sfec_result.get("invoices", []):
            seller_inv = inv.get("seller_invoice_number", "") or inv.get("invoice_number", "")
            if seller_inv:
                sfec_ids.add(seller_inv)
    except Exception:
        pass
    uncertified = [
        inv for inv in cache.get("sales_invoices", [])
        if inv.get("numero", "") not in sfec_ids
        and inv.get("valide", 0) == 1
        and inv.get("sfec_statut", "") not in ("EN_COURS", "ERREUR")
    ]
    uncertified.sort(key=lambda x: x.get("date_facture", ""), reverse=True)
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)
    rows = ""
    for inv in uncertified[:200]:
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), inv.get("id", ""), inv.get("id", "")
        )
    sfec_warn = ""
    if not sfec_cfg.get("enabled"):
        sfec_warn = '<p class="badge badge-err" style="margin-bottom:12px">SFEC desactivee</p>'
    empty = '<tr><td colspan="5" style="text-align:center;color:#64748b">Toutes certifiees !</td></tr>' if not rows else ""
    body = """
<div class="card"><h2>En attente de certification SFEC ({count})</h2>{sfec_warn}
<p style="font-size:13px;color:#94a3b8;margin-bottom:12px">{val_count} facture(s) a comptabiliser (DO_Statut=2)</p>
<table><thead><tr><th>Numero</th><th>Date</th><th>Tiers</th>
<th style="text-align:right">Montant TTC</th><th>Action</th></tr></thead>
<tbody>{rows}{empty}</tbody></table>
<div id="cert-status" style="margin-top:12px"></div></div>
<script>
function certifySingle(id){{
document.getElementById("cert-status").textContent="Certification en cours...";
api("POST","/api/sfec/certify",{{invoice_id:id}}).then(function(d){{
if(d.success){{
showToast("Certifie: "+d.certification_number,"ok");
setTimeout(function(){{ location.reload(); }}, 1500);
}} else {{
showToast("Erreur: "+d.error,"err");
if(d.queued){{ document.getElementById("cert-status").innerHTML='<span class="queue-badge">Mis en file d attente (offline)</span>'; }}
}}
}});
}}
function toMonitor(id){{
document.getElementById("cert-status").textContent="Marquage a surveiller...";
api("POST","/api/sfec/to-monitor",{{invoice_id:id}}).then(function(d){{
if(d.success){{
showToast("Marque a surveiller","warn");
setTimeout(function(){{ location.reload(); }}, 1500);
}} else {{
showToast("Erreur: "+d.error,"err");
}}
}});
}}
</script>""".format(
        count=len(uncertified),
        sfec_warn=sfec_warn, val_count=val_count, rows=rows, empty=empty
    )
    return _page(body)


@app.route("/certified")
@_login_required
def certified_page():
    cache = get_cache()
    sfec_invoices = cache.get("sfec_invoices", [])
    last_sfec = cache.get("last_sfec_sync_at", "")

    rows = ""
    for inv in sfec_invoices:
        invoice_num = inv.get("invoice_number", "")
        cert_date = (inv.get("certification_date") or "")[:10]
        short_sig = inv.get("certification_short_signature", "")
        buyer = inv.get("buyer_name", "")
        amount = inv.get("total_ttc", "0")
        try:
            amount_fmt = "{:,.0f}".format(float(amount))
        except (ValueError, TypeError):
            amount_fmt = str(amount)
        sfec_id = inv.get("id", "")
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td class='no-print'><a href='/certified/{}/print' class='btn btn-sm btn-primary' target='_blank'>Imprimer</a></td></tr>".format(
            _esc(invoice_num), _esc(short_sig), _esc(buyer), _esc(amount_fmt), sfec_id
        )
    empty = '<tr><td colspan="5" style="text-align:center;color:#64748b">Aucune facture certifiee - clique sur Sync</td></tr>' if not rows else ""
    loading = ""
    if not sfec_invoices and not last_sfec:
        loading = '<p style="color:#f59e0b">Chargement en cours... ( Rafraichir dans quelques secondes )</p>'
    body = """
<div class="card"><h2>Factures certifiees SFEC ({count})</h2>
<button class="btn btn-sm btn-success no-print" onclick="refreshSfec()" id="sfec-refresh">Sync SFEC</button>
<span id="sfec-status" style="margin-left:8px;font-size:12px;color:#64748b">Derniere sync: {last_sfec}</span>
{loading}
<table style="margin-top:12px"><thead><tr><th>Facture</th><th>N certif</th><th>Client</th><th style="text-align:right">Montant TTC</th><th class="no-print">Action</th></tr></thead>
<tbody>{rows}{empty}</tbody></table></div>
<script>
function refreshSfec(){{
  document.getElementById("sfec-status").textContent="Chargement en cours...";
  document.getElementById("sfec-refresh").disabled=true;
  api("POST","/api/sfec/sync",{{}}).then(function(d){{
    location.reload();
  }}).catch(function(e){{ document.getElementById("sfec-status").textContent="Erreur: "+e; document.getElementById("sfec-refresh").disabled=false; }});
}}
setTimeout(function(){{ if(document.querySelector("td[colspan='5']")) location.reload(); }}, 5000);
</script>""".format(count=len(sfec_invoices), rows=rows, empty=empty, loading=loading,
                   last_sfec=last_sfec[:19].replace("T"," ") if last_sfec else "jamais")
    return _page(body)


@app.route("/certified/<invoice_id>/print")
@_login_required
def print_certified(invoice_id):
    inv = None
    try:
        client = SfecClient()
        inv = client.get_invoice(invoice_id)
    except Exception as e:
        logger.warning("Impossible de recuperer la facture SFEC %s: %s", invoice_id, e)

    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/certified'>Retour</a></p>", 404

    company = get_config().get("company", {})
    qr_code = inv.get("qr_code") or inv.get("certification_qr_code") or ""
    is_image = qr_code.startswith("data:")
    if qr_code:
        qr_display = '<img src="{}" style="max-width:200px;max-height:200px">'.format(qr_code) if is_image else '<pre style="font-size:10px;word-break:break-all">{}</pre>'.format(qr_code)
    else:
        qr_display = '<p style="color:#999">QR Code non disponible</p>'

    items = inv.get("items_json") or []
    item_rows = ""
    for item in items:
        item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(item.get("designation", "")),
            item.get("quantity", 0),
            item.get("unit_price", 0),
            _esc(item.get("tax_rate", "0")),
            item.get("net_amount", 0),
        )

    body = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Facture {invoice_number} - SFEC</title>
<style>
body{{font-family:'Segoe UI',sans-serif;padding:40px;color:#000;background:#fff}}
.header{{display:flex;justify-content:space-between;margin-bottom:30px;border-bottom:2px solid #333;padding-bottom:20px}}
.company-name{{font-size:24px;font-weight:bold}}
.company-info{{font-size:12px;color:#666}}
.cert-box{{background:#f0f0f0;border:2px solid #333;padding:16px;margin:20px 0;border-radius:4px}}
.cert-box h3{{margin-bottom:8px;font-size:14px}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left;font-size:13px}}
th{{background:#f5f5f5}}
.total-row td{{border-top:2px solid #333;font-weight:bold;font-size:14px}}
.qr-section{{text-align:center;margin:30px 0;padding:20px;border:1px dashed #999}}
.footer{{margin-top:40px;font-size:11px;color:#666;border-top:1px solid #ccc;padding-top:10px}}
@media print{{.no-print{{display:none!important}}}}
</style></head><body>
<div class="header">
<div>
<div class="company-name">{seller_name}</div>
<div class="company-info">{seller_addr}</div>
<div class="company-info">NIU: {seller_niu}</div>
<div class="company-info">RCCM: {seller_rccm}</div>
</div>
<div style="text-align:right">
<h2>FACTURE</h2>
<div class="company-info">Numero: {invoice_number}</div>
<div class="company-info">Date: {invoice_date}</div>
<div class="company-info">Devise: {currency}</div>
</div>
</div>

<div style="display:flex;justify-content:space-between;margin-bottom:20px">
<div style="border:1px solid #ccc;padding:12px;border-radius:4px;flex:1;margin-right:8px">
<h3 style="font-size:13px;margin-bottom:8px">Client</h3>
<div><strong>{buyer_name}</strong></div>
<div class="company-info">NIU: {buyer_niu}</div>
<div class="company-info">{buyer_addr}</div>
<div class="company-info">{buyer_phone}</div>
</div>
<div style="border:1px solid #ccc;padding:12px;border-radius:4px;flex:1;margin-left:8px">
<h3 style="font-size:13px;margin-bottom:8px">Paiement</h3>
<div>Methode: {payment_method}</div>
<div>Montant du: <strong>{amount_due} {currency}</strong></div>
</div>
</div>

<table>
<thead><tr><th>Designation</th><th style="text-align:right">Qte</th><th style="text-align:right">Prix unit.</th><th style="text-align:right">TVA</th><th style="text-align:right">Montant HT</th></tr></thead>
<tbody>{item_rows}
<tr class="total-row"><td colspan="4">Total HT</td><td style="text-align:right">{total_ht} {currency}</td></tr>
<tr class="total-row"><td colspan="4">TVA 18%</td><td style="text-align:right">{total_tax18} {currency}</td></tr>
<tr class="total-row"><td colspan="4">TVA 5%</td><td style="text-align:right">{total_tax5} {currency}</td></tr>
<tr class="total-row"><td colspan="4"><strong>TOTAL TTC</strong></td><td style="text-align:right"><strong>{total_ttc} {currency}</strong></td></tr>
</tbody></table>

<div class="cert-box">
<h3>SFEC - Systeme de Facturation Electronique Certifie</h3>
<table>
<tr><td><strong>Statut certification</strong></td><td>{cert_status}</td></tr>
<tr><td><strong>Signature courte</strong></td><td>{short_sig}</td></tr>
<tr><td><strong>Signature complete</strong></td><td style="word-break:break-all;font-size:10px">{signature}</td></tr>
<tr><td><strong>Date de certification</strong></td><td>{cert_date}</td></tr>
</table>
</div>

<div class="qr-section">
<h3>QR Code de certification</h3>
{qr_display}
</div>

<div class="footer no-print">
<button class="btn btn-primary" onclick="window.print()">Imprimer</button>
<a href="/certified" style="margin-left:8px;color:#38bdf8">Retour</a>
</div>
</body></html>""".format(
        invoice_number=_esc(inv.get("invoice_number", "")),
        invoice_date=_esc((inv.get("invoice_date") or "")[:10]),
        currency=_esc(inv.get("currency", "XAF")),
        seller_name=_esc(inv.get("seller_name", company.get("name", ""))),
        seller_addr=_esc(inv.get("seller_address", company.get("address", ""))),
        seller_niu=_esc(inv.get("seller_niu", company.get("tax_number", ""))),
        seller_rccm=_esc(inv.get("seller_rccm", "")),
        buyer_name=_esc(inv.get("buyer_name", "")),
        buyer_niu=_esc(inv.get("buyer_niu", "")),
        buyer_addr=_esc(inv.get("buyer_address", "")),
        buyer_phone=_esc(inv.get("buyer_phone", "")),
        payment_method=_esc(inv.get("payment_method", "")),
        amount_due=_esc(inv.get("amount_due", "0")),
        total_ht=_esc(inv.get("total_ht", "0")),
        total_tax18=_esc(inv.get("total_tax18", "0")),
        total_tax5=_esc(inv.get("total_tax5", "0")),
        total_ttc=_esc(inv.get("total_ttc", "0")),
        item_rows=item_rows,
        cert_status=_esc(inv.get("certification_status", "")),
        short_sig=_esc(inv.get("certification_short_signature", "")),
        signature=_esc(inv.get("certification_signature", "")),
        cert_date=_esc((inv.get("certification_date") or "")[:19].replace("T", " ")),
        qr_display=qr_display,
    )
    return body


# ══════════════════════════════════════════════════════════════
# CONFIG PAGE - 7 ONGLETS
# ══════════════════════════════════════════════════════════════

@app.route("/config")
@_login_required
def config_page():
    cfg = get_config()
    db = cfg.get("db", {})
    sfec = cfg.get("sfec", {})
    company = cfg.get("company", {})
    dash = cfg.get("dashboard", {})
    queue = cfg.get("queue", {})
    validation = cfg.get("validation", {})
    notifications = cfg.get("notifications", {})
    rate_limit = cfg.get("rate_limit", {})
    log_level = cfg.get("log_level", "info")

    def _s(val, target):
        return "selected" if val == target else ""

    def _ck(val):
        return "checked" if val else ""

    body = """<div class="card"><h2>Configuration</h2>
<div class="tabs">
<button class="tab-btn active" id="tab-btn-db" onclick="switchTab('db')">Base de Donnees</button>
<button class="tab-btn" id="tab-btn-sfec" onclick="switchTab('sfec')">API SFEC</button>
<button class="tab-btn" id="tab-btn-company" onclick="switchTab('company')">Entreprise</button>
<button class="tab-btn" id="tab-btn-sync" onclick="switchTab('sync')">Synchronisation</button>
<button class="tab-btn" id="tab-btn-validation" onclick="switchTab('validation')">Validation & Limites</button>
<button class="tab-btn" id="tab-btn-notif" onclick="switchTab('notif')">Notifications</button>
<button class="tab-btn" id="tab-btn-system" onclick="switchTab('system')">Systeme</button>
</div>"""

    # ── TAB 1: DATABASE ──
    body += """
<div class="tab-content active" id="tab-db">
<h2 style="margin-bottom:16px">Base de donnees Sage 100</h2>
<form id="form-db" onsubmit="event.preventDefault();saveForm('form-db','/api/config/db')">
<div class="grid-2">
<div><label>Serveur</label><input type="text" name="server" value="{server}"></div>
<div><label>Port</label><input type="number" name="port" value="{port}"></div>
<div><label>Base de donnees</label><input type="text" name="database" value="{database}"><div class="desc">Nom exact de la base Sage 100</div></div>
<div><label>Utilisateur</label><input type="text" name="user" value="{user}"></div>
<div><label>Mot de passe</label><input type="password" name="password" value="{password}"></div>
<div><label>Connexion Windows</label><select name="trusted_connection"><option value="false" {tc_f}>Non</option><option value="true" {tc_t}>Oui</option></select></div>
<div><label>Chiffrer</label><select name="encrypt"><option value="false" {enc_f}>Non</option><option value="true" {enc_t}>Oui</option></select></div>
<div><label>Confiance certificat</label><select name="trust_server_certificate"><option value="true" {tsc_t}>Oui</option><option value="false" {tsc_f}>Non</option></select></div>
</div>
<h2 style="margin-top:16px">Pool & Timeouts</h2>
<div class="grid-2">
<div><label>Connexions max</label><input type="number" name="pool_max" value="{pool_max}"></div>
<div><label>Connexions min</label><input type="number" name="pool_min" value="{pool_min}"></div>
<div><label>Idle timeout (ms)</label><input type="number" name="pool_idle_timeout_ms" value="{pool_idle_timeout_ms}"></div>
<div><label>Connection timeout (ms)</label><input type="number" name="connection_timeout_ms" value="{connection_timeout_ms}"></div>
<div><label>Request timeout (ms)</label><input type="number" name="request_timeout_ms" value="{request_timeout_ms}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-success" onclick="testDb()">Tester la connexion</button>
<div id="db-test" style="margin-top:8px"></div>
</div>
</form></div>""".format(
        server=_esc(db.get("server", "")), port=_esc(db.get("port", 1433)), database=_esc(db.get("database", "")),
        user=_esc(db.get("user", "")), password=_esc(db.get("password", "")),
        tc_f=_s(db.get("trusted_connection"), False), tc_t=_s(db.get("trusted_connection"), True),
        enc_f=_s(db.get("encrypt"), False), enc_t=_s(db.get("encrypt"), True),
        tsc_t=_s(db.get("trust_server_certificate"), True), tsc_f=_s(db.get("trust_server_certificate"), False),
        pool_max=db.get("pool_max", 10), pool_min=db.get("pool_min", 2),
        pool_idle_timeout_ms=db.get("pool_idle_timeout_ms", 30000),
        connection_timeout_ms=db.get("connection_timeout_ms", 15000),
        request_timeout_ms=db.get("request_timeout_ms", 30000),
    )

    # ── TAB 2: SFEC ──
    body += """
<div class="tab-content" id="tab-sfec">
<h2 style="margin-bottom:16px">Configuration API SFEC</h2>
<form id="form-sfec" onsubmit="event.preventDefault();saveForm('form-sfec','/api/config/sfec')">
<div class="grid-2">
<div><label>Cle API SFEC</label><input type="text" name="api_key" value="{api_key}"></div>
<div><label>Environnement</label><select name="use_sandbox"><option value="true" {sb_t}>Sandbox (test)</option><option value="false" {sb_f}>Production</option></select></div>
<div><label>SFEC active</label><select name="enabled"><option value="true" {en_t}>Oui</option><option value="false" {en_f}>Non</option></select></div>
<div><label>Statut pour certification</label><select name="certify_status">
<option value="SAISI" {cs_sa}>SAISI</option>
<option value="CONFIRME" {cs_co}>CONFIRME</option>
<option value="A COMPTABILISER" {cs_ac}>A COMPTABILISER</option>
<option value="COMPTABILISE" {cs_c}>COMPTABILISE</option>
<option value="LETTRÉ" {cs_l}>LETTRÉ</option></select></div>
<div><label>URL Production</label><input type="text" name="base_url" value="{base_url}"></div>
<div><label>URL Sandbox</label><input type="text" name="sandbox_url" value="{sandbox_url}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-success" onclick="testSfec()">Tester la connexion SFEC</button>
<div id="sfec-test" style="margin-top:8px"></div>
</div>
</form></div>""".format(
        api_key=_esc(sfec.get("api_key", "")),
        sb_t=_s(sfec.get("use_sandbox"), True), sb_f=_s(sfec.get("use_sandbox"), False),
        en_t=_s(sfec.get("enabled"), True), en_f=_s(sfec.get("enabled"), False),
        cs_sa=_s(sfec.get("certify_status"), "SAISI"),
        cs_co=_s(sfec.get("certify_status"), "CONFIRME"),
        cs_ac=_s(sfec.get("certify_status"), "A COMPTABILISER"),
        cs_c=_s(sfec.get("certify_status"), "COMPTABILISE"),
        cs_l=_s(sfec.get("certify_status"), "LETTRÉ"),
        base_url=_esc(sfec.get("base_url", "")), sandbox_url=_esc(sfec.get("sandbox_url", "")),
    )

    # ── TAB 3: COMPANY ──
    body += """
<div class="tab-content" id="tab-company">
<h2 style="margin-bottom:16px">Configuration Entreprise</h2>
<form id="form-company" onsubmit="event.preventDefault();saveForm('form-company','/api/config/company')">
<div class="grid-2">
<div><label>Nom de l'entreprise</label><input type="text" name="name" value="{name}"></div>
<div><label>NIU (Tax Number)</label><input type="text" name="tax_number" value="{tax_number}"></div>
<div><label>Adresse</label><input type="text" name="address" value="{address}"></div>
<div><label>Ville</label><input type="text" name="city" value="{city}"></div>
<div><label>Pays</label><input type="text" name="country" value="{country}"></div>
<div><label>Telephone</label><input type="text" name="phone" value="{phone}"></div>
<div><label>Email</label><input type="email" name="email" value="{email}"></div>
<div><label>Devise</label><select name="currency"><option value="XAF" {cur_xaf}>XAF (FCFA)</option><option value="USD" {cur_usd}>USD</option></select></div>
</div>
<h2 style="margin-top:16px">Banque</h2>
<div class="grid-2">
<div><label>Banque</label><input type="text" name="bank_name" value="{bank_name}"></div>
<div><label>Compte</label><input type="text" name="bank_account" value="{bank_account}"></div>
<div><label>IBAN</label><input type="text" name="bank_iban" value="{bank_iban}"></div>
<div><label>SWIFT</label><input type="text" name="bank_swift" value="{bank_swift}"></div>
</div>
<h2 style="margin-top:16px">Facturation</h2>
<div class="grid-2">
<div><label>Prefixe facture</label><input type="text" name="invoice_prefix" value="{invoice_prefix}"></div>
<div><label>Numero suivant</label><input type="number" name="invoice_next_number" value="{invoice_next_number}"></div>
<div><label>Notes facture</label><input type="text" name="invoice_notes" value="{invoice_notes}"></div>
<div><label>Pied de page</label><input type="text" name="invoice_footer" value="{invoice_footer}"></div>
<div><label>Regime fiscal</label><input type="text" name="tax_regime" value="{tax_regime}"></div>
<div><label>QR Code active</label><select name="qrcode_enabled"><option value="true" {qr_t}>Oui</option><option value="false" {qr_f}>Non</option></select></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        name=_esc(company.get("name", "")), tax_number=_esc(company.get("tax_number", "")),
        address=_esc(company.get("address", "")), city=_esc(company.get("city", "")),
        country=_esc(company.get("country", "")), phone=_esc(company.get("phone", "")),
        email=_esc(company.get("email", "")),
        cur_xaf=_s(company.get("currency"), "XAF"), cur_usd=_s(company.get("currency"), "USD"),
        bank_name=_esc(company.get("bank_name", "")), bank_account=_esc(company.get("bank_account", "")),
        bank_iban=_esc(company.get("bank_iban", "")), bank_swift=_esc(company.get("bank_swift", "")),
        invoice_prefix=_esc(company.get("invoice_prefix", "INV-")),
        invoice_next_number=_esc(company.get("invoice_next_number", 1)),
        invoice_notes=_esc(company.get("invoice_notes", "")),
        invoice_footer=_esc(company.get("invoice_footer", "")),
        tax_regime=_esc(company.get("tax_regime", "")),
        qr_t=_s(company.get("qrcode_enabled"), True), qr_f=_s(company.get("qrcode_enabled"), False),
    )

    # ── TAB 4: SYNC ──
    body += """
<div class="tab-content" id="tab-sync">
<h2 style="margin-bottom:16px">Synchronisation & Polling</h2>
<form id="form-sync" onsubmit="event.preventDefault();saveForm('form-sync','/api/config/sync')">
<div class="grid-2">
<div><label>Polling active</label><select name="polling_enabled"><option value="true" {pe_t}>Oui</option><option value="false" {pe_f}>Non</option></select><div class="desc">Active/desactive la synchro automatique</div></div>
<div><label>Intervalle polling (ms)</label><input type="number" name="polling_interval_ms" value="{poll_ms}"><div class="desc">Recommande: 30000 (30 secondes)</div></div>
</div>
<h2 style="margin-top:16px">File d'attente</h2>
<div class="grid-2">
<div><label>Concurrence max</label><input type="number" name="max_concurrent" value="{max_concurrent}"><div class="desc">Certifications SFEC simultanees</div></div>
<div><label>Retries max</label><input type="number" name="max_retries" value="{max_retries}"><div class="desc">Nombre de tentatives en cas d'echec</div></div>
<div><label>Delai retry (ms)</label><input type="number" name="retry_base_delay_ms" value="{retry_base_delay_ms}"><div class="desc">Delai de base entre les retries</div></div>
<div><label>Taille max queue</label><input type="number" name="max_queue_size" value="{max_queue_size}"></div>
<div><label>Timeout job (ms)</label><input type="number" name="job_timeout_ms" value="{job_timeout_ms}"></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        pe_t=_s(db.get("polling_enabled"), True), pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
        max_concurrent=queue.get("max_concurrent", 5),
        max_retries=queue.get("max_retries", 4),
        retry_base_delay_ms=queue.get("retry_base_delay_ms", 2000),
        max_queue_size=queue.get("max_queue_size", 10000),
        job_timeout_ms=queue.get("job_timeout_ms", 60000),
    )

    # ── TAB 5: VALIDATION & LIMITES ──
    notif_events = notifications.get("events", [])
    body += """
<div class="tab-content" id="tab-validation">
<h2 style="margin-bottom:16px">Validation</h2>
<form id="form-validation" onsubmit="event.preventDefault();saveForm('form-validation','/api/config/validation')">
<div class="grid-2">
<div><label>Mode strict</label><select name="strict_mode"><option value="true" {st_t}>Oui</option><option value="false" {st_f}>Non</option></select><div class="desc">Verification stricte des donnees avant certification</div></div>
<div><label>Tolerance montant</label><input type="number" name="tolerance_amount" value="{tolerance}" step="0.01"><div class="desc">Ecart autorise entre les totaux</div></div>
</div>
<h2 style="margin-top:16px">Limites API SFEC</h2>
<div class="grid-2">
<div><label>Maximum journalier</label><input type="number" name="daily_max" value="{daily_max}"><div class="desc">Nombre max de certifications par jour</div></div>
<div><label>Maximum par minute</label><input type="number" name="per_minute_max" value="{per_minute_max}"></div>
<div><label>Seuil d'alerte (%)</label><input type="number" name="alert_threshold_percent" value="{alert_threshold}"><div class="desc">Declenche une alerte a ce %</div></div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        st_t=_s(validation.get("strict_mode"), True), st_f=_s(validation.get("strict_mode"), False),
        tolerance=validation.get("tolerance_amount", 0.01),
        daily_max=rate_limit.get("daily_max", 2500),
        per_minute_max=rate_limit.get("per_minute_max", 100),
        alert_threshold=rate_limit.get("alert_threshold_percent", 80),
    )

    # ── TAB 6: NOTIFICATIONS ──
    body += """
<div class="tab-content" id="tab-notif">
<h2 style="margin-bottom:16px">Notifications</h2>
<form id="form-notif" onsubmit="event.preventDefault();saveForm('form-notif','/api/config/notifications')">
<div class="grid-2">
<div><label>Notifications activees</label><select name="enabled"><option value="true" {en_t}>Oui</option><option value="false" {en_f}>Non</option></select></div>
</div>
<h2 style="margin-top:16px">Evenements</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
<div><input type="checkbox" name="evt_synced" value="invoice:synced" {ev_synced}> Facture synchro</div>
<div><input type="checkbox" name="evt_val_error" value="invoice:validation_error" {ev_val}> Erreur validation</div>
<div><input type="checkbox" name="evt_dlq" value="queue:dlq_added" {ev_dlq}> File d'attente</div>
<div><input type="checkbox" name="evt_db_fail" value="db:connection_failed" {ev_dbf}> Echec connexion DB</div>
<div><input type="checkbox" name="evt_db_err" value="db:polling_error" {ev_dbe}> Erreur polling</div>
</div>
<div style="margin-top:16px"><button type="submit" class="btn btn-primary">Sauvegarder</button></div>
</form></div>""".format(
        en_t=_s(notifications.get("enabled"), True), en_f=_s(notifications.get("enabled"), False),
        ev_synced=_ck("invoice:synced" in notif_events),
        ev_val=_ck("invoice:validation_error" in notif_events),
        ev_dlq=_ck("queue:dlq_added" in notif_events),
        ev_dbf=_ck("db:connection_failed" in notif_events),
        ev_dbe=_ck("db:polling_error" in notif_events),
    )

    # ── TAB 7: SYSTEM ──
    body += """
<div class="tab-content" id="tab-system">
<h2 style="margin-bottom:16px">Systeme & Dashboard</h2>
<form id="form-system" onsubmit="event.preventDefault();saveForm('form-system','/api/config/system')">
<div class="grid-2">
<div><label>Niveau de log</label><select name="log_level">
<option value="debug" {ll_d}>Debug</option>
<option value="info" {ll_i}>Info</option>
<option value="warning" {ll_w}>Warning</option>
<option value="error" {ll_e}>Error</option></select></div>
<div><label>Port dashboard</label><input type="number" name="port" value="{port}"></div>
<div><label>Auth active</label><select name="auth_enabled"><option value="true" {auth_t}>Oui</option><option value="false" {auth_f}>Non</option></select></div>
<div><label>Email login</label><input type="email" name="login_email" value="{login_email}"></div>
<div><label>Mot de passe login</label><input type="password" name="login_password" value="{login_password}"></div>
<div><label>Session secret</label><input type="text" name="session_secret" value="{session_secret}"></div>
<div><label>Duree session (heures)</label><input type="number" name="session_max_age_hours" value="{session_max_hours}"></div>
<div><label>Host</label><input type="text" name="host" value="{host}"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Sauvegarder</button>
<button type="button" class="btn btn-warning" onclick="applyConfig()" style="margin-left:8px">Appliquer (recharger)</button>
</div>
</form>
<h2 style="margin-top:24px">Export / Import Configuration</h2>
<div style="margin-top:8px">
<a href="/api/config/export" class="btn btn-success" download="config.json">Exporter config.json</a>
<button type="button" class="btn btn-primary" onclick="document.getElementById('import-file').click()" style="margin-left:8px">Importer config.json</button>
<input type="file" id="import-file" accept=".json" style="display:none" onchange="importConfig(this)">
</div>
<div id="import-status" style="margin-top:8px"></div>
</div>""".format(
        ll_d=_s(log_level, "debug"), ll_i=_s(log_level, "info"),
        ll_w=_s(log_level, "warning"), ll_e=_s(log_level, "error"),
        port=_esc(dash.get("port", 3000)),
        auth_t=_s(dash.get("auth_enabled"), True), auth_f=_s(dash.get("auth_enabled"), False),
        login_email=_esc(dash.get("login_email", "")), login_password=_esc(dash.get("login_password", "")),
        session_secret=_esc(dash.get("session_secret", "")),
        session_max_hours=_esc(dash.get("session_max_age_hours", 24)),
        host=_esc(dash.get("host", "0.0.0.0")),
    )

    body += """
<script>
function testDb(){document.getElementById("db-test").textContent="Test en cours...";api("GET","/api/db/test").then(function(d){document.getElementById("db-test").innerHTML=d.ok?'<span class="badge badge-ok">Connecte: '+d.table_map.documents+'</span>':'<span class="badge badge-err">'+d.error+'</span>'})}
function testSfec(){document.getElementById("sfec-test").textContent="Test en cours...";api("GET","/api/sfec/test").then(function(d){document.getElementById("sfec-test").innerHTML=d.connected?'<span class="badge badge-ok">Connecte: '+d.url+'</span>':'<span class="badge badge-err">Deconnecte</span>'})}
function applyConfig(){showToast("Application de la configuration...","info");api("POST","/api/config/reload").then(function(d){showToast(d.message||"OK","ok");}).catch(function(e){showToast("Erreur: "+e,"err")})}
function importConfig(input){var file=input.files[0];if(!file)return;var reader=new FileReader();reader.onload=function(e){try{var cfg=JSON.parse(e.target.result);document.getElementById("import-status").textContent="Importation...";api("POST","/api/config/import",cfg).then(function(d){if(d.ok){showToast("Config importee! Rechargement...","ok");setTimeout(function(){location.reload()},1500)}else{showToast("Erreur: "+d.error,"err")}})}catch(err){showToast("JSON invalide: "+err,"err")}};reader.readAsText(file)}
</script>"""

    return _page(body)


# ══════════════════════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════════════════════

def _bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


@app.route("/api/metrics")
@_login_required
def api_metrics():
    return jsonify(get_metrics())


@app.route("/api/connectivity")
@_login_required
def api_connectivity():
    return jsonify(get_status())


@app.route("/api/sync", methods=["POST"])
@_login_required
def api_sync():
    threading.Thread(target=sync_all, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync lancee"})


@app.route("/api/articles/sync", methods=["POST"])
@_login_required
def api_articles_sync():
    def _run():
        try:
            import article_sync
            result = article_sync.sync_articles_from_sage()
            logger.info("Sync articles API: %s", result)
        except Exception as e:
            logger.error("Sync articles API: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync des articles lancee"})


@app.route("/api/db/test")
@_login_required
def api_db_test():
    return jsonify(ping_database())


@app.route("/api/sfec/test")
@_login_required
def api_sfec_test():
    return jsonify(check_health())


@app.route("/api/sfec/certify", methods=["POST"])
@_login_required
def api_sfec_certify():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400

    if str(invoice_id).startswith("INV-"):
        try:
            inv_num = int(str(invoice_id).replace("INV-", ""))
        except ValueError:
            return jsonify({"success": False, "error": "Format ID invalide: {}".format(invoice_id)}), 400

        inv = invoice_engine.get_invoice(inv_num)
        if not inv:
            return jsonify({"success": False, "error": "Facture SQLite #{} non trouvee".format(inv_num)}), 404

        try:
            result = certify_sqlite_invoice(inv)
            return jsonify(result)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(certify_single(invoice_id))


@app.route("/api/sfec/to-monitor", methods=["POST"])
@_login_required
def api_sfec_to_monitor():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400
    try:
        mark_to_monitor(invoice_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sfec/debug", methods=["POST"])
@_login_required
def api_sfec_debug():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    return jsonify({"ok": True, "payload": payload, "invoice_data": {k: v for k, v in invoice.items() if k != "lignes"}})


@app.route("/api/sfec/validate", methods=["POST"])
@_login_required
def api_sfec_validate():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    errors = validate_sfec_payload(payload)
    return jsonify({"ok": len(errors) == 0, "errors": errors, "payload": payload})


@app.route("/api/sfec/sync", methods=["POST"])
@_login_required
def api_sfec_sync():
    def _do_sync():
        sync_sfec_invoices()
    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Sync lancee en arriere-plan"})


@app.route("/api/sfec/certified-from-api")
@_login_required
def api_sfec_certified_from_api():
    try:
        client = SfecClient()
        result = client.list_invoices(page=1, page_size=100)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contacts")
@_login_required
def api_contacts():
    return jsonify(fetch_contacts())


@app.route("/api/tax-rates")
@_login_required
def api_tax_rates():
    return jsonify(fetch_tax_rates())


@app.route("/api/ledger-accounts")
@_login_required
def api_ledger_accounts():
    return jsonify(fetch_ledger_accounts())


@app.route("/api/tables")
@_login_required
def api_tables():
    return jsonify(list_all_tables())


@app.route("/api/certified")
@_login_required
def api_certified():
    return jsonify(fetch_certified_invoices())


@app.route("/api/retry-queue")
@_login_required
def api_retry_queue():
    return jsonify(get_retry_queue())


@app.route("/api/config", methods=["GET"])
@_login_required
def api_get_config():
    cfg = get_config()
    masked = dict(cfg)
    for section in ("db", "sfec"):
        if section in masked:
            masked[section] = dict(masked[section])
            for key in ("password", "api_key"):
                if key in masked[section]:
                    masked[section][key] = "***"
    return jsonify(masked)


# ── Config Save Endpoints ──

@app.route("/api/config/db", methods=["POST"])
@_login_required
def api_config_db():
    cfg = get_config()
    db = dict(cfg.get("db", {}))
    data = request.get_json(silent=True) or {}
    for key in ("server", "database", "user", "password"):
        if key in data:
            db[key] = data[key]
    for key in ("port", "polling_interval_ms", "pool_max", "pool_min",
                "pool_idle_timeout_ms", "connection_timeout_ms", "request_timeout_ms"):
        if key in data:
            try:
                db[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    for key in ("trusted_connection", "encrypt", "trust_server_certificate", "polling_enabled"):
        if key in data:
            db[key] = _bool(data[key])
    cfg["db"] = db
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/sfec", methods=["POST"])
@_login_required
def api_config_sfec():
    cfg = get_config()
    sfec = dict(cfg.get("sfec", {}))
    data = request.get_json(silent=True) or {}
    for key in ("api_key", "base_url", "sandbox_url", "certify_status"):
        if key in data:
            sfec[key] = data[key]
    for key in ("use_sandbox", "enabled"):
        if key in data:
            sfec[key] = _bool(data[key])
    cfg["sfec"] = sfec
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/company", methods=["POST"])
@_login_required
def api_config_company():
    cfg = get_config()
    company = dict(cfg.get("company", {}))
    data = request.get_json(silent=True) or {}
    str_fields = ("name", "tax_number", "address", "city", "country", "phone", "email",
                  "website", "rc_number", "bank_name", "bank_account", "bank_iban", "bank_swift",
                  "currency", "slogan", "default_payment_terms",
                  "invoice_prefix", "invoice_notes", "invoice_footer", "tax_regime")
    for key in str_fields:
        if key in data:
            company[key] = data[key]
    if "invoice_next_number" in data:
        try:
            company["invoice_next_number"] = int(data["invoice_next_number"])
        except (ValueError, TypeError):
            pass
    for key in ("qrcode_enabled",):
        if key in data:
            company[key] = _bool(data[key])
    cfg["company"] = company
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/sync", methods=["POST"])
@_login_required
def api_config_sync():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    if "polling_interval_ms" in data:
        try:
            db["polling_interval_ms"] = int(data["polling_interval_ms"])
        except (ValueError, TypeError):
            pass
    cfg["db"] = db

    queue = dict(cfg.get("queue", {}))
    for key in ("max_concurrent", "max_retries", "retry_base_delay_ms", "max_queue_size", "job_timeout_ms"):
        if key in data:
            try:
                queue[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["queue"] = queue

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/validation", methods=["POST"])
@_login_required
def api_config_validation():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    validation = dict(cfg.get("validation", {}))
    if "strict_mode" in data:
        validation["strict_mode"] = _bool(data["strict_mode"])
    if "tolerance_amount" in data:
        try:
            validation["tolerance_amount"] = float(data["tolerance_amount"])
        except (ValueError, TypeError):
            pass
    cfg["validation"] = validation

    rate_limit = dict(cfg.get("rate_limit", {}))
    for key in ("daily_max", "per_minute_max", "alert_threshold_percent"):
        if key in data:
            try:
                rate_limit[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["rate_limit"] = rate_limit

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/notifications", methods=["POST"])
@_login_required
def api_config_notifications():
    cfg = get_config()
    data = request.get_json(silent=True) or {}
    notifications = dict(cfg.get("notifications", {}))
    if "enabled" in data:
        notifications["enabled"] = _bool(data["enabled"])

    events = []
    event_keys = {
        "evt_synced": "invoice:synced",
        "evt_val_error": "invoice:validation_error",
        "evt_dlq": "queue:dlq_added",
        "evt_db_fail": "db:connection_failed",
        "evt_db_err": "db:polling_error",
    }
    for form_key, event_name in event_keys.items():
        if form_key in data and _bool(data[form_key]):
            events.append(event_name)
    notifications["events"] = events

    cfg["notifications"] = notifications
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/system", methods=["POST"])
@_login_required
def api_config_system():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    if "log_level" in data:
        cfg["log_level"] = data["log_level"]

    dash = dict(cfg.get("dashboard", {}))
    for key in ("login_email", "login_password", "session_secret", "host"):
        if key in data:
            dash[key] = data[key]
    if "port" in data:
        try:
            dash["port"] = int(data["port"])
        except (ValueError, TypeError):
            pass
    if "auth_enabled" in data:
        dash["auth_enabled"] = _bool(data["auth_enabled"])
    if "session_max_age_hours" in data:
        try:
            dash["session_max_age_hours"] = int(data["session_max_age_hours"])
        except (ValueError, TypeError):
            pass
    cfg["dashboard"] = dash

    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/dashboard", methods=["POST"])
@_login_required
def api_config_dashboard():
    return api_config_system()


@app.route("/api/config/reload", methods=["POST"])
@_login_required
def api_config_reload():
    try:
        result = apply_config()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config/export")
@_login_required
def api_config_export():
    from flask import send_file, after_this_request
    import tempfile
    cfg = get_config()
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(cfg, tmp, indent=2, ensure_ascii=False)
    tmp.close()
    tmp_path = tmp.name

    @after_this_request
    def cleanup(response):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name="config.json", mimetype="application/json")


@app.route("/api/config/import", methods=["POST"])
@_login_required
def api_config_import():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Donnees JSON invalides (objet requis)"}), 400

    allowed_sections = {"db", "sfec", "company", "dashboard", "queue",
                        "validation", "notifications", "rate_limit", "log_level"}
    unknown = set(data.keys()) - allowed_sections
    if unknown:
        return jsonify({"ok": False, "error": "Sections inconnues: {}".format(", ".join(sorted(unknown)))}), 400

    required_sections = ("db", "sfec", "company")
    for section in required_sections:
        if section not in data:
            return jsonify({"ok": False, "error": "Section '{}' manquante".format(section)}), 400
        if not isinstance(data[section], dict):
            return jsonify({"ok": False, "error": "La section '{}' doit etre un objet".format(section)}), 400

    try:
        save_config(data)
        return jsonify({"ok": True, "message": "Configuration importee avec succes"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# PHASE 3+4 : FACTURATION & POS
# ══════════════════════════════════════════════════════════════

@app.route("/billing")
@_login_required
def billing_page():
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()
    inv_list = invoice_engine.list_invoices(limit=50)
    rows = ""
    for inv in inv_list["invoices"]:
        s = inv.get("statut", "")
        sb = "badge-ok" if s in ("valide", "a_comptabilise") else "badge-warn" if s in ("a_comptabiliser",) else ""
        ss = inv.get("sfec_statut", "")
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-warn" if ss == "EN_COURS" else "badge-err" if ss == "ERREUR" else "badge-info" if ss else ""
        sftxt = "Certifie" if ss == "CERTIFIE" else "Deja certifie" if ss == "DEJA_CERTIFIE" else "En cours" if ss == "EN_COURS" else "Erreur" if ss == "ERREUR" else "A certifier" if ss else "-"
        src = "badge-info" if inv.get("source") == "sage" else ""
        srctxt = inv.get("source", "web")
        rows += "<tr><td><a href='/billing/invoice/{}' style='color:#38bdf8;text-decoration:none'>{}</a></td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.0f}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td></tr>".format(
            inv["id"], _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")),
            _esc(inv.get("tiers_nom", "")), inv.get("montant_ttc", 0),
            sb, _esc(s), sfb, _esc(sftxt), src, _esc(srctxt)
        )
    empty = '<tr><td colspan="7" style="text-align:center;color:#64748b">Aucune facture - <a href="/billing/invoice/new" style="color:#38bdf8">Creer une facture</a></td></tr>' if not rows else ""
    body = """
<div class="card"><h2>Facturation</h2><div class="stat-grid">
<div class="stat"><div class="value">{total}</div><div class="label">Total factures</div></div>
<div class="stat"><div class="value">{brouillons}</div><div class="label">Brouillons</div></div>
<div class="stat"><div class="value">{validees}</div><div class="label">Validees</div></div>
<div class="stat"><div class="value">{certifiees}</div><div class="label">Certifiees SFEC</div></div>
<div class="stat"><div class="value">{ca:,.0f}</div><div class="label">CA Total (FCFA)</div></div>
<div class="stat"><div class="value">{pushed}</div><div class="label">Sync Sage OK</div></div>
</div></div>
<div class="card no-print"><h2>Actions</h2>
<a href="/billing/invoice/new" class="btn btn-primary">Nouvelle Facture</a>
<button class="btn btn-success" onclick="syncBi()" style="margin-left:8px">Sync Sage</button>
<span id="sync-status" style="margin-left:8px;font-size:12px;color:#64748b">Derniere sync: {last_sync}</span>
</div>
<div class="card"><h2>Factures ({count})</h2>
<table><thead><tr><th>Numero</th><th>Date</th><th>Tiers</th><th style='text-align:right'>Montant TTC</th><th>Statut</th><th>SFEC</th><th>Source</th></tr></thead>
<tbody>{rows}{empty}</tbody></table></div>
<script>
function syncBi(){{document.getElementById("sync-status").textContent="Sync en cours...";api("POST","/api/sync/bi",{{}}).then(function(d){{showToast("Sync: "+d.pushed+" poussees, "+d.pull_new+" tirees","ok");setTimeout(function(){{location.reload()}},1500)}}).catch(function(e){{showToast("Erreur: "+e,"err")}})}}
</script>""".format(
        total=stats.get("total", 0), brouillons=stats.get("brouillons", 0),
        validees=stats.get("validees", 0), certifiees=stats.get("certifiees", 0),
        ca=stats.get("ca_total", 0), pushed=bi_stats.get("pushed", 0),
        last_sync=bi_stats.get("last_sync", "jamais")[:19].replace("T", " ") if bi_stats.get("last_sync") else "jamais",
        count=inv_list.get("total", 0), rows=rows, empty=empty
    )
    return _page(body)


@app.route("/billing/invoice/new")
@_login_required
def invoice_new_page():
    return _invoice_form_page(None)


@app.route("/billing/invoice/<int:invoice_id>")
@_login_required
def invoice_detail_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    lignes_rows = ""
    for l in inv.get("lignes", []):
        lignes_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.2f}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(l.get("designation", "")), l.get("quantite", 1), l.get("prix_unitaire", 0),
            _esc("{}%".format(l.get("taux_tva", 18))), l.get("montant_ht", 0), l.get("montant_tva", 0), l.get("montant_ttc", 0)
        )
    ss = inv.get("sfec_statut", "")
    sfec_html = ""
    if ss:
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-err" if ss == "ERREUR" else "badge-warn"
        sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(sfb, _esc(ss), _esc(inv.get("sfec_num_certif", "")[:30] or "-"))
    body = """
<div class="card"><h2>Facture {}</h2>
<div style="margin-bottom:12px">
<a href="/billing" class="btn btn-sm">Retour</a>
<a href="/billing/invoice/{}/edit" class="btn btn-sm btn-primary" style="margin-left:8px">Modifier</a>
<button class="btn btn-sm btn-success" onclick="certifyInv({})" style="margin-left:8px">Certifier SFEC</button>
<a href="/api/invoices/{}/pdf" class="btn btn-sm btn-primary" style="margin-left:8px;text-decoration:none" target="_blank">PDF</a>
<button class="btn btn-sm btn-danger" onclick="if(confirm('Supprimer ?')){{api('DELETE','/api/invoices/{}').then(function(){{location.href='/billing'}})}}" style="margin-left:8px">Supprimer</button>
</div>
<table>
<tr><td>Numero</td><td><strong>{}</strong></td></tr>
<tr><td>Date</td><td>{}</td></tr>
<tr><td>Reference</td><td>{}</td></tr>
<tr><td>Tiers</td><td>{} ({})</td></tr>
<tr><td>Statut</td><td><span class="badge badge-ok">{}</span></td></tr>
<tr><td>Source</td><td>{}</td></tr>
{}
<tr><td>Notes</td><td>{}</td></tr>
</table>
<h3 style="margin-top:16px;color:#38bdf8">Lignes</h3>
<table><thead><tr><th>Designation</th><th style='text-align:right'>Qte</th><th style='text-align:right'>Prix unit.</th><th style='text-align:right'>TVA</th><th style='text-align:right'>HT</th><th style='text-align:right'>TVA</th><th style='text-align:right'>TTC</th></tr></thead>
<tbody>{}</tbody></table>
<div style="text-align:right;margin-top:12px;font-size:18px">
<strong>Total HT: {:,.0f} FCFA</strong><br>
<strong>TVA: {:,.0f} FCFA</strong><br>
<strong style="color:#38bdf8">TOTAL TTC: {:,.0f} FCFA</strong>
</div></div>
<script>
function certifyInv(id){{api("POST","/api/sfec/certify",{{invoice_id:"INV-"+id}}).then(function(d){{if(d.success){{showToast("Certifie: "+d.certification_number,"ok");setTimeout(function(){{location.reload()}},1500)}}else{{showToast("Erreur: "+d.error,"err")}}}})}}
</script>""".format(
        _esc(inv.get("numero", "")),
        inv["id"], inv["id"], inv["id"],
        _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")),
        _esc(inv.get("reference", "") or "-"), _esc(inv.get("tiers_nom", "") or "N/A"),
        _esc(inv.get("tiers_code", "") or ""), _esc(inv.get("statut", "")),
        _esc(inv.get("source", "web")),
        sfec_html, _esc(inv.get("notes", "") or "-"),
        lignes_rows, inv.get("montant_ht", 0), inv.get("montant_tva", 0), inv.get("montant_ttc", 0)
    )
    return _page(body)


@app.route("/billing/invoice/<int:invoice_id>/edit")
@_login_required
def invoice_edit_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    return _invoice_form_page(inv)


def _invoice_form_page(inv):
    is_edit = inv is not None
    title = "Modifier Facture" if is_edit else "Nouvelle Facture"
    numero = inv.get("numero", "") if is_edit else ""
    date_facture = inv.get("date_facture", "") if is_edit else ""
    date_echeance = inv.get("date_echeance", "") if is_edit else ""
    reference = inv.get("reference", "") if is_edit else ""
    tiers_code = inv.get("tiers_code", "") if is_edit else ""
    tiers_nom = inv.get("tiers_nom", "") if is_edit else ""
    tiers_niu = inv.get("tiers_niu", "") if is_edit else ""
    tiers_email = inv.get("tiers_email", "") if is_edit else ""
    tiers_telephone = inv.get("tiers_telephone", "") if is_edit else ""
    tiers_adresse = inv.get("tiers_adresse", "") if is_edit else ""
    tiers_type = inv.get("tiers_type", "business") if is_edit else "business"
    notes = inv.get("notes", "") if is_edit else ""
    statut = inv.get("statut", "brouillon") if is_edit else "brouillon"

    lignes_json = json.dumps(inv.get("lignes", [])) if is_edit else "[]"
    contacts_json = json.dumps(pos_engine.list_contacts(limit=200))
    products_json = json.dumps(pos_engine.list_products(limit=200))
    tax_rates_json = json.dumps(pos_engine.list_tax_rates())

    body = """
<div class="page-header">
  <div>
    <div class="breadcrumb"><a href="/billing">Facturation</a> / {title}</div>
    <h1>{title}</h1>
  </div>
  <div>
    <a href="/billing" class="btn btn-sm btn-primary">Retour</a>
  </div>
</div>

<form id="form-invoice">
  <div class="invoice-layout">
    <div class="invoice-main">
      <!-- Invoice Header -->
      <div class="invoice-header-card">
        <div class="section-title">Informations facture</div>
        <div class="invoice-meta">
          <div><label>Numero</label><input type="text" name="numero" value="{numero}" {readonly}></div>
          <div><label>Date facture</label><input type="date" name="date_facture" value="{date_facture}"></div>
          <div><label>Echeance</label><input type="date" name="date_echeance" value="{date_echeance}"></div>
          <div><label>Reference</label><input type="text" name="reference" value="{reference}"></div>
          <div><label>Statut</label><select name="statut">
            <option value="brouillon" {s_brouillon}>Brouillon</option>
            <option value="valide" {s_valide}>Valide</option>
            <option value="a_comptabiliser" {s_acompt}>A comptabiliser</option>
          </select></div>
          <div><label>Type tiers</label><select name="tiers_type">
            <option value="business" {t_bus}>Entreprise</option>
            <option value="individual" {t_ind}>Particulier</option>
            <option value="government" {t_gov}>Gouvernement</option>
            <option value="foreign" {t_for}>Etranger</option>
          </select></div>
        </div>
      </div>

      <!-- Client Section -->
      <div class="client-card">
        <div class="section-title">Client / Tiers</div>
        <div class="grid-2">
          <div class="contact-select-wrapper">
            <label>Selectionner un contact</label>
            <select id="contact-select" onchange="fillContact()">
              <option value="">-- Choisir --</option>
            </select>
          </div>
          <div><label>Code tiers</label><input type="text" id="tiers_code" name="tiers_code" value="{tiers_code}"></div>
          <div><label>Nom</label><input type="text" id="tiers_nom" name="tiers_nom" value="{tiers_nom}"></div>
          <div><label>NIU</label><input type="text" id="tiers_niu" name="tiers_niu" value="{tiers_niu}"></div>
          <div><label>Email</label><input type="email" id="tiers_email" name="tiers_email" value="{tiers_email}"></div>
          <div><label>Telephone</label><input type="text" id="tiers_telephone" name="tiers_telephone" value="{tiers_telephone}"></div>
          <div class="full-width"><label>Adresse</label><input type="text" id="tiers_adresse" name="tiers_adresse" value="{tiers_adresse}"></div>
        </div>
      </div>

      <!-- Line Items -->
      <div style="margin-top:20px">
        <div class="section-title">Lignes de facture</div>
        <div id="lines-container"></div>
        <button type="button" class="btn btn-sm" onclick="addLine()" style="margin-top:10px">+ Ajouter une ligne</button>
      </div>

      <!-- Notes -->
      <div style="margin-top:20px">
        <div class="section-title">Notes</div>
        <textarea name="notes" rows="3" style="width:100%;background:#fff;border:1.5px solid #e2e8f0;color:#1e293b;padding:10px 14px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">{notes}</textarea>
      </div>

      <!-- Actions -->
      <div class="form-actions">
        <button type="submit" class="btn btn-primary">{btn_text}</button>
        <button type="button" class="btn btn-success" onclick="saveAndCertify()">Enregistrer & Certifier SFEC</button>
      </div>
    </div>

    <!-- Sidebar Totals -->
    <div class="invoice-sidebar">
      <div class="totals-panel">
        <h3>Recapitulatif</h3>
        <div class="total-row">
          <span class="total-label">Total HT</span>
          <span class="total-value"><span id="total-ht">0</span> FCFA</span>
        </div>
        <div class="total-row">
          <span class="total-label">TVA</span>
          <span class="total-value"><span id="total-tva">0</span> FCFA</span>
        </div>
        <div class="total-row grand-total">
          <span class="total-label">TOTAL TTC</span>
          <span class="total-value"><span id="total-ttc">0</span> FCFA</span>
        </div>
      </div>
    </div>
  </div>
</form>
<script>
var contacts={contacts_json};
var products={products_json};
var taxRates={tax_rates_json};
var existingLines={lignes_json};
var isEdit={is_edit};
var invoiceId={inv_id};
var lineCounter=0;

function fillContact(){{var sel=document.getElementById("contact-select");var c=contacts.find(function(x){{return x.code===sel.value}});if(c){{document.getElementById("tiers_code").value=c.code;document.getElementById("tiers_nom").value=c.nom;document.getElementById("tiers_niu").value=c.niu||"";document.getElementById("tiers_email").value=c.email||"";document.getElementById("tiers_telephone").value=c.telephone||"";document.getElementById("tiers_adresse").value=c.adresse||""}}}}

function addLine(data){{lineCounter++;var idx=lineCounter;var html='<div class="line-item" id="line-'+idx+'"><div class="line-item-header"><span class="line-item-title">Ligne #'+idx+'</span><div class="line-item-actions"><button type="button" class="btn btn-sm btn-danger" onclick="removeLine('+idx+')">Supprimer</button></div></div><div class="line-item-grid"><div><label>Designation</label><input type="text" name="l_design_'+idx+'" value="'+(data?data.designation:"")+'" list="products-list"></div><div><label>Qte</label><input type="number" name="l_qte_'+idx+'" value="'+(data?data.quantite:1)+'" step="0.01" min="0" onchange="calcLine('+idx+')"></div><div><label>Prix unitaire</label><input type="number" name="l_prix_'+idx+'" value="'+(data?data.prix_unitaire:0)+'" step="0.01" min="0" onchange="calcLine('+idx+')"></div><div><label>TVA %</label><select name="l_tva_'+idx+'" onchange="calcLine('+idx+')"></select></div><div><label>HT</label><input type="text" id="l_ht_'+idx+'" readonly value="'+(data?data.montant_ht:0)+'"></div><div><label>TTC</label><input type="text" id="l_ttc_'+idx+'" readonly value="'+(data?data.montant_ttc:0)+'"></div></div><input type="hidden" name="l_code_article_'+idx+'" value="'+(data?data.code_article:"")+'"><input type="hidden" name="l_famille_'+idx+'" value="'+(data?data.famille:"")+'"></div>';
document.getElementById("lines-container").insertAdjacentHTML("beforeend",html);
var sel=document.querySelector("select[name='l_tva_"+idx+"']");taxRates.forEach(function(t){{sel.innerHTML+='<option value="'+t.taux+'"'+(data&&data.taux_tva==t.taux?" selected":"")+'>'+t.code+' ('+t.taux+'%)</option>'}});
if(data)calcLine(idx)}}

function removeLine(idx){{var el=document.getElementById("line-"+idx);if(el)el.remove();calcTotals()}}

function calcLine(idx){{var q=parseFloat(document.querySelector("input[name='l_qte_"+idx+"']").value)||0;var p=parseFloat(document.querySelector("input[name='l_prix_"+idx+"']").value)||0;var t=parseFloat(document.querySelector("select[name='l_tva_"+idx+"']").value)||0;var ht=round2(q*p);var tva=round2(ht*t/100);var ttc=round2(ht+tva);document.getElementById("l_ht_"+idx).value=ht;document.getElementById("l_ttc_"+idx).value=ttc;calcTotals()}}

function calcTotals(){{var th=0,tt=0;document.querySelectorAll("[id^='l_ht_']").forEach(function(el){{th+=parseFloat(el.value)||0}});document.querySelectorAll("[id^='l_ttc_']").forEach(function(el){{tt+=parseFloat(el.value)||0}});document.getElementById("total-ht").textContent=round2(th).toLocaleString();document.getElementById("total-tva").textContent=round2(tt-th).toLocaleString();document.getElementById("total-ttc").textContent=round2(tt).toLocaleString()}}
function round2(n){{return Math.round(n*100)/100}}

function gatherData(){{var d={{}};d.date_facture=document.querySelector("input[name='date_facture']").value;d.date_echeance=document.querySelector("input[name='date_echeance']").value;d.reference=document.querySelector("input[name='reference']").value;d.tiers_code=document.getElementById("tiers_code").value;d.tiers_nom=document.getElementById("tiers_nom").value;d.tiers_niu=document.getElementById("tiers_niu").value;d.tiers_email=document.getElementById("tiers_email").value;d.tiers_telephone=document.getElementById("tiers_telephone").value;d.tiers_adresse=document.getElementById("tiers_adresse").value;d.tiers_type=document.querySelector("select[name='tiers_type']").value;d.statut=document.querySelector("select[name='statut']").value;d.notes=document.querySelector("textarea[name='notes']").value;d.lignes=[];var lines=document.querySelectorAll("[id^='line-']");lines.forEach(function(el){{var idx=el.id.replace("line-","");var ligne={{}};ligne.designation=document.querySelector("input[name='l_design_"+idx+"']").value;ligne.quantite=parseFloat(document.querySelector("input[name='l_qte_"+idx+"']").value)||1;ligne.prix_unitaire=parseFloat(document.querySelector("input[name='l_prix_"+idx+"']").value)||0;ligne.taux_tva=parseFloat(document.querySelector("select[name='l_tva_"+idx+"']").value)||18;ligne.code_article=document.querySelector("input[name='l_code_article_"+idx+"']").value;ligne.famille=document.querySelector("input[name='l_famille_"+idx+"']").value;if(ligne.designation)d.lignes.push(ligne)}});return d}}

document.getElementById("form-invoice").onsubmit=function(e){{e.preventDefault();var d=gatherData();var url=isEdit?"/api/invoices/"+invoiceId:"/api/invoices";var method=isEdit?"PUT":"POST";api(method,url,d).then(function(r){{if(r.success||r.id){{showToast("Facture sauvegardee","ok");setTimeout(function(){{location.href="/billing/invoice/"+(r.id||invoiceId)}},1000)}}else{{showToast("Erreur: "+(r.error||"inconnue"),"err")}}}}).catch(function(e){{showToast("Erreur reseau: "+e,"err")}})}}

function saveAndCertify(){{var d=gatherData();d._certify=true;var url=isEdit?"/api/invoices/"+invoiceId:"/api/invoices";var method=isEdit?"PUT":"POST";api(method,url,d).then(function(r){{if(r.id){{showToast("Facture sauvegardee, certification...","info");api("POST","/api/sfec/certify",{{invoice_id:"INV-"+r.id}}).then(function(c){{if(c.success){{showToast("Certifie: "+c.certification_number,"ok")}}else{{showToast("Certification: "+c.error,"warn")}}setTimeout(function(){{location.href="/billing/invoice/"+r.id}},1500)}})}}}})}}

if(existingLines.length>0){{existingLines.forEach(function(l){{addLine(l)}})}}
else{{addLine()}}
</script>
<datalist id="products-list"></datalist>
""".format(
        title=title, numero=_esc(numero), date_facture=date_facture, date_echeance=date_echeance,
        reference=_esc(reference), tiers_code=_esc(tiers_code), tiers_nom=_esc(tiers_nom),
        tiers_niu=_esc(tiers_niu), tiers_email=_esc(tiers_email), tiers_telephone=_esc(tiers_telephone),
        tiers_adresse=_esc(tiers_adresse), notes=_esc(notes),
        s_brouillon="selected" if statut == "brouillon" else "",
        s_valide="selected" if statut == "valide" else "",
        s_acompt="selected" if statut == "a_comptabiliser" else "",
        t_bus="selected" if tiers_type == "business" else "",
        t_ind="selected" if tiers_type == "individual" else "",
        t_gov="selected" if tiers_type == "government" else "",
        t_for="selected" if tiers_type == "foreign" else "",
        readonly="readonly" if is_edit else "",
        contacts_json=contacts_json, products_json=products_json, tax_rates_json=tax_rates_json,
        lignes_json=lignes_json, is_edit="true" if is_edit else "false",
        inv_id=inv["id"] if is_edit else "null",
        btn_text="Modifier" if is_edit else "Enregistrer"
    )
    return _page(body)


# ── API INVOICES ──

@app.route("/api/invoices", methods=["GET"])
@_login_required
def api_list_invoices():
    result = invoice_engine.list_invoices(
        type_doc=request.args.get("type"),
        statut=request.args.get("statut"),
        source=request.args.get("source"),
        search=request.args.get("search"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=min(int(request.args.get("limit", 200)), 500),
        offset=int(request.args.get("offset", 0))
    )
    return jsonify(result)


@app.route("/api/invoices", methods=["POST"])
@_login_required
def api_create_invoice():
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.create_invoice(data)
        if data.get("_certify"):
            pass
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/<int:invoice_id>", methods=["GET"])
@_login_required
def api_get_invoice(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Non trouvee"}), 404
    return jsonify(inv)


@app.route("/api/invoices/<int:invoice_id>", methods=["PUT"])
@_login_required
def api_update_invoice(invoice_id):
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.update_invoice(invoice_id, data)
        if not result:
            return jsonify({"error": "Non trouvee"}), 404
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/<int:invoice_id>", methods=["DELETE"])
@_login_required
def api_delete_invoice(invoice_id):
    try:
        ok = invoice_engine.delete_invoice(invoice_id)
        if not ok:
            return jsonify({"error": "Non trouvee"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/invoices/stats")
@_login_required
def api_invoice_stats():
    return jsonify(invoice_engine.count_invoices())


# ── SYNC BIDIRECTIONNELLE ──

@app.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    try:
        result = sync_bidirectional.full_sync()
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync/bi/stats")
@_login_required
def api_sync_bi_stats():
    return jsonify(sync_bidirectional.get_sync_stats())


# ── CONTACTS & PRODUCTS ──

@app.route("/api/contacts", methods=["GET"])
@_login_required
def api_list_contacts():
    contacts = pos_engine.list_contacts(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"contacts": contacts, "total": len(contacts)})


@app.route("/api/contacts", methods=["POST"])
@_login_required
def api_create_contact():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_contact(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/products", methods=["GET"])
@_login_required
def api_list_products():
    products = pos_engine.list_products(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"products": products, "total": len(products)})


@app.route("/api/products", methods=["POST"])
@_login_required
def api_create_product():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_product(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/tax-rates/local")
@_login_required
def api_local_tax_rates():
    return jsonify(pos_engine.list_tax_rates())


# ══════════════════════════════════════════════════════════════
# PHASE 4 : MODULE VENTE / POS
# ══════════════════════════════════════════════════════════════

@app.route("/pos")
@_login_required
def pos_page():
    stats = pos_engine.count_tickets()
    products = pos_engine.list_products(limit=50)
    tax_rates = pos_engine.list_tax_rates()
    tickets_list = pos_engine.list_tickets(limit=20)
    current_vendeur_id = session.get("vendeur_id")

    prod_rows = ""
    for p in products:
        prod_rows += '<div class="stat" style="cursor:pointer;min-width:120px" onclick="addPosProduct({})"><div class="value" style="font-size:14px">{}</div><div class="label">{:,.0f} FCFA</div></div>'.format(
            json.dumps({"ref": p["ref"], "designation": p["designation"], "prix_vente": p["prix_vente"], "tva_code": p.get("tva_code", "18")})[:200],
            _esc(p["designation"][:20]), p.get("prix_vente", 0)
        )

    products_json_str = json.dumps(products)[:8000]
    tax_rates_json_str = json.dumps(tax_rates)
    vendeurs = pos_engine.list_vendeurs()
    vendeurs_json_str = json.dumps([{"id":v["id"],"nom":v["nom"],"prenom":v.get("prenom","")} for v in vendeurs])

    ticket_rows_html = ""
    for t in tickets_list["tickets"]:
        ticket_rows_html += "<tr><td>{}</td><td>{}</td><td style='text-align:right'>{:,.0f}</td><td>{}</td></tr>".format(
            _esc(t.get("numero", "")), _esc(t.get("date_ticket", "")[:10]),
            t.get("montant_ttc", 0), _esc(t.get("mode_paiement", ""))
        )
    if not ticket_rows_html:
        ticket_rows_html = '<tr><td colspan="4" style="text-align:center;color:#64748b">Aucun ticket</td></tr>'

    vendeur_opts_html = '<option value="">Sans vendeur</option>'
    for v in vendeurs:
        label = "{} {}".format(v.get("prenom", "") or "", v.get("nom", "") or "").strip()
        sel = ' selected' if current_vendeur_id and v["id"] == current_vendeur_id else ""
        vendeur_opts_html += '<option value="{}"{}>{}</option>'.format(v["id"], sel, label)

    body = """
<div class="grid-2">
<div>
<div class="card"><h2>Module de Vente</h2>
<div class="stat-grid" style="margin-bottom:12px">
<div class="stat"><div class="value">""" + str(stats.get("tickets_jour", 0)) + """</div><div class="label">Tickets aujourd'hui</div></div>
<div class="stat"><div class="value">""" + "{:,.0f}".format(stats.get("ca_jour", 0)) + """</div><div class="label">CA Jour (FCFA)</div></div>
<div class="stat"><div class="value">""" + "{:,.0f}".format(stats.get("ca_total", 0)) + """</div><div class="label">CA Total (FCFA)</div></div>
</div></div>
<div class="card"><h2>Nouveau Ticket</h2>
<div class="grid-2">
<div><label>Client</label><input type="text" id="pos-client" value="Client comptoir"></div>
<div><label>Paiement</label><select id="pos-paiement">
<option value="especes">Especes</option>
<option value="mobile_money">Mobile Money</option>
<option value="virement">Virement</option>
<option value="carte">Carte</option>
</select></div>
<div style="grid-column:1/-1"><label>Vendeur</label><select id="pos-vendeur">""" + vendeur_opts_html + """</select></div>
</div>
<div style="margin-top:16px;background:#fff;border:1.5px solid #dbeafe;border-radius:12px;padding:16px">
<label style="color:#3b82f6;font-weight:700;font-size:14px">Scanner code-barres</label>
<input type="text" id="pos-barcode" placeholder="Scannez ou tapez le code-barres..." autofocus
  style="width:100%;padding:14px;font-size:18px;font-weight:700;letter-spacing:2px;margin-top:8px;background:#f8fafc;border:1.5px solid #cbd5e1;color:#1e293b;border-radius:8px;text-align:center"
  onkeydown="if(event.key==='Enter'){event.preventDefault();scanBarcode(this.value);this.value='';this.focus()}">
</div>
<div style="margin-top:12px">
<label>Ajouter / Rechercher</label>
<div style="display:flex;gap:8px">
<input type="text" id="pos-search" placeholder="Rechercher par nom ou ref..." onkeyup="searchPosProducts()" style="flex:1">
<input type="number" id="pos-qte" value="1" min="1" style="width:60px">
<button type="button" class="btn btn-sm btn-primary" onclick="addPosLineManual()">+ Manuel</button>
</div>
<div id="pos-product-list" style="max-height:150px;overflow-y:auto;margin-top:8px"></div>
</div>
<div id="pos-cart" style="margin-top:12px">
<table id="pos-cart-table"><thead><tr><th>Article</th><th style="text-align:right">Qte</th><th style="text-align:right">Prix</th><th style="text-align:right">Total</th><th></th></tr></thead>
<tbody id="pos-cart-body"></tbody></table>
</div>
<div style="text-align:right;margin-top:12px;font-size:20px">
<strong>Total: <span id="pos-total">0</span> FCFA</strong>
</div>
<div style="margin-top:12px;display:flex;gap:8px">
<label style="margin:0">Recu:</label>
<input type="number" id="pos-recu" value="0" min="0" style="width:120px" onchange="calcMonnaie()">
<span id="pos-monnaie" style="color:#10b981;font-weight:bold"></span>
</div>
<div style="margin-top:12px">
<button class="btn btn-success" onclick="validerTicket()" style="width:100%;padding:12px;font-size:16px">Valider & Imprimer</button>
</div>
</div>
</div>
<div>
<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><h2 style="margin:0">Articles rapides</h2><button class="btn btn-sm btn-primary" onclick="syncPosArticles()">Sync articles</button></div>
<div class="stat-grid" id="pos-products-grid">
""" + prod_rows + """
</div></div>
<div class="card"><h2>Derniers tickets</h2>
<table><thead><tr><th>Numero</th><th>Date</th><th style="text-align:right">TTC</th><th>Paiement</th></tr></thead>
<tbody id="pos-tickets-list">""" + ticket_rows_html + """</tbody></table></div>
</div>
</div>
<script>
var posProducts=""" + products_json_str + """;
var posTaxRates=""" + tax_rates_json_str + """;
var posVendeurs=""" + vendeurs_json_str + """;
var posCart=[];
var posLineIdx=0;

function scanBarcode(code){if(!code||!code.trim())return;fetch("/api/products/barcode?code="+encodeURIComponent(code.trim())).then(function(r){return r.json()}).then(function(d){if(d.found){var p=d.product;var q=parseInt(document.getElementById("pos-qte").value)||1;posLineIdx++;posCart.push({idx:posLineIdx,ref:p.ref,barcode:p.barcode||"",designation:p.designation,prix_unitaire:p.prix_vente,quantite:q,taux_tva:parseFloat(p.tva_code)||18,product_id:p.id});renderPosCart();showToast(p.designation+" ajoute","ok")}else{showToast("Code-barres inconnu: "+code,"warn")}}).catch(function(){showToast("Erreur scan","err")})}

function addPosProduct(p){var q=parseInt(document.getElementById("pos-qte").value)||1;posLineIdx++;posCart.push({idx:posLineIdx,ref:p.ref,barcode:p.barcode||"",designation:p.designation,prix_unitaire:p.prix_vente,quantite:q,taux_tva:parseFloat(p.tva_code)||18,product_id:p.id||null});renderPosCart()}

function addPosLineManual(){var desc=prompt("Designation:");if(!desc)return;var prix=parseFloat(prompt("Prix unitaire:"))||0;var q=parseInt(document.getElementById("pos-qte").value)||1;var tva=parseFloat(prompt("Taux TVA (%):","18"))||18;posLineIdx++;posCart.push({idx:posLineIdx,ref:"",barcode:"",designation:desc,prix_unitaire:prix,quantite:q,taux_tva:tva});renderPosCart()}

function removePosLine(idx){posCart=posCart.filter(function(l){return l.idx!==idx});renderPosCart()}

function renderPosCart(){var body="";var total=0;posCart.forEach(function(l){var ttc=round2(l.quantite*l.prix_unitaire*(1+l.taux_tva/100));total+=ttc;body+="<tr><td>"+l.designation+"</td><td style='text-align:right'><input type='number' value='"+l.quantite+"' min='1' style='width:50px' onchange='updatePosQte("+l.idx+",this.value)'></td><td style='text-align:right'>"+l.prix_unitaire.toLocaleString()+"</td><td style='text-align:right'>"+ttc.toLocaleString()+"</td><td><button class='btn-sm btn-danger' onclick='removePosLine("+l.idx+")'>X</button></td></tr>"});document.getElementById("pos-cart-body").innerHTML=body;document.getElementById("pos-total").textContent=total.toLocaleString();calcMonnaie()}

function updatePosQte(idx,val){var l=posCart.find(function(x){return x.idx===idx});if(l){l.quantite=parseInt(val)||1;renderPosCart()}}

function calcMonnaie(){var total=0;posCart.forEach(function(l){total+=round2(l.quantite*l.prix_unitaire*(1+l.taux_tva/100))});var recu=parseFloat(document.getElementById("pos-recu").value)||0;var monnaie=recu-total;document.getElementById("pos-monnaie").textContent=monnaie>=0?"Monnaie: "+monnaie.toLocaleString()+" FCFA":"Manque: "+Math.abs(monnaie).toLocaleString()+" FCFA"}

function searchPosProducts(){var q=document.getElementById("pos-search").value.toLowerCase();var html="";posProducts.forEach(function(p){if(!q||p.designation.toLowerCase().indexOf(q)>=0||p.ref.toLowerCase().indexOf(q)>=0||(p.barcode&&p.barcode.toLowerCase().indexOf(q)>=0)){html+="<div style='padding:6px;cursor:pointer;border-bottom:1px solid #e2e8f0' onclick='addPosProduct("+JSON.stringify(p).replace(/"/g,"&quot;")+")'>"+p.ref+" - "+p.designation+" - "+p.prix_vente.toLocaleString()+" FCFA</div>"}});document.getElementById("pos-product-list").innerHTML=html}

function validerTicket(){if(posCart.length===0){showToast("Panier vide","err");return}var lignes=[];posCart.forEach(function(l){lignes.push({designation:l.designation,quantite:l.quantite,prix_unitaire:l.prix_unitaire,taux_tva:l.taux_tva,code_article:l.ref,barcode:l.barcode||"",product_id:l.product_id||null})});var vid=document.getElementById("pos-vendeur").value;var data={tiers_nom:document.getElementById("pos-client").value,mode_paiement:document.getElementById("pos-paiement").value,montant_recu:parseFloat(document.getElementById("pos-recu").value)||0,vendeur_id:vid?parseInt(vid):null,lignes:lignes};api("POST","/api/pos/ticket",data).then(function(d){if(d.success){showToast("Ticket "+d.numero+" cree! Monnaie: "+d.monnaie_rendue.toLocaleString()+" FCFA","ok");posCart=[];posLineIdx=0;renderPosCart();document.getElementById("pos-recu").value=0;document.getElementById("pos-client").value="Client comptoir";document.getElementById("pos-barcode").value="";document.getElementById("pos-barcode").focus();setTimeout(function(){location.href="/pos/ticket/"+d.id+"/print"},1500)}else{showToast("Erreur: "+d.error,"err")}}).catch(function(e){showToast("Erreur: "+e,"err")})}

function round2(n){return Math.round(n*100)/100}

function syncPosArticles(){showToast("Sync des articles en cours...","info");api("POST","/api/articles/sync",{}).then(function(d){showToast(d.message||"OK","ok");setTimeout(function(){location.reload()},2500)}).catch(function(e){showToast("Erreur: "+e,"err")})}
</script>"""
    return _page(body)


@app.route("/pos/ticket/<int:ticket_id>/print")
@_login_required
def pos_print_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return "<h1>Ticket non trouve</h1>", 404

    company = get_config().get("company", {})
    lignes_rows = ""
    for l in ticket.get("lignes", []):
        lignes_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(l.get("designation", "")), l.get("quantite", 1),
            l.get("prix_unitaire", 0), l.get("montant_ttc", 0)
        )

    logo_html = ""
    logo_path = company.get("logo_file_name", "")
    if logo_path and os.path.exists(logo_path):
        logo_html = '<img src="{}" style="max-height:60px">'.format(logo_path)

    body = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Ticket {numero}</title>
<style>
body{{font-family:'Segoe UI',sans-serif;padding:20px;color:#000;background:#fff;max-width:400px;margin:0 auto}}
.header{{text-align:center;margin-bottom:20px}}
.logo{{margin-bottom:10px}}
.company{{font-size:18px;font-weight:bold}}
.info{{font-size:11px;color:#666}}
.ticket-info{{border-top:2px solid #333;padding-top:10px;margin-top:10px}}
table{{width:100%;border-collapse:collapse;margin:10px 0}}
th,td{{padding:4px 0;font-size:12px}}
th{{border-bottom:1px solid #333;text-align:left}}
.total{{border-top:2px solid #333;font-weight:bold;font-size:14px}}
.footer{{text-align:center;margin-top:20px;font-size:10px;color:#999}}
@media print{{body{{padding:10px;max-width:100%}}.no-print{{display:none!important}}}}
</style></head><body>
<div class="header">
<div class="logo">{logo}</div>
<div class="company">{company_name}</div>
<div class="info">{company_addr}</div>
<div class="info">NIU: {company_niu}</div>
<div class="info">Tel: {company_tel}</div>
</div>
<div class="ticket-info">
<strong>TICKET DE VENTE</strong><br>
Numero: <strong>{numero}</strong><br>
Date: {date}<br>
Caissier: {caissier}<br>
Client: {client}
</div>
<table>
<thead><tr><th>Article</th><th style="text-align:right">Qte</th><th style="text-align:right">Prix</th><th style="text-align:right">Total</th></tr></thead>
<tbody>{lignes}</tbody>
<tr class="total"><td colspan="3">TOTAL TTC</td><td style="text-align:right">{total:,.0f} FCFA</td></tr>
</table>
<div style="margin-top:10px;font-size:12px">
Mode: {paiement}<br>
Recu: {recu:,.0f} FCFA<br>
<strong>Monnaie: {monnaie:,.0f} FCFA</strong>
</div>
<div class="footer">
{message}
</div>
<div class="no-print" style="margin-top:20px;text-align:center">
<button onclick="window.print()" style="padding:10px 20px;background:#38bdf8;border:none;border-radius:4px;font-size:14px;cursor:pointer">Imprimer</button>
<a href="/api/pos/ticket/{ticket_id}/pdf" style="margin-left:12px;padding:10px 20px;background:#10b981;color:white;border:none;border-radius:4px;font-size:14px;text-decoration:none" target="_blank">PDF</a>
<a href="/pos" style="margin-left:12px;color:#38bdf8">Retour</a>
</div>
</body></html>""".format(
        numero=_esc(ticket.get("numero", "")),
        logo=logo_html,
        company_name=_esc(company.get("name", "Mon Entreprise")),
        company_addr=_esc(company.get("address", "")),
        company_niu=_esc(company.get("tax_number", "")),
        company_tel=_esc(company.get("phone", "")),
        date=_esc(ticket.get("date_ticket", "")),
        caissier=_esc(ticket.get("caissier", "")),
        client=_esc(ticket.get("tiers_nom", "")),
        lignes=lignes_rows,
        total=ticket.get("montant_ttc", 0),
        paiement=_esc(ticket.get("mode_paiement", "")),
        recu=ticket.get("montant_recu", 0),
        monnaie=ticket.get("monnaie_rendue", 0),
        message=get_setting("ticket_message", "Merci pour votre achat !"),
        ticket_id=ticket["id"]
    )
    return body


# ── API POS ──

@app.route("/api/pos/ticket", methods=["POST"])
@_login_required
def api_create_ticket():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_ticket(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/pos/tickets", methods=["GET"])
@_login_required
def api_list_tickets():
    result = pos_engine.list_tickets(
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=min(int(request.args.get("limit", 100)), 500)
    )
    return jsonify(result)


@app.route("/api/pos/ticket/<int:ticket_id>", methods=["GET"])
@_login_required
def api_get_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(ticket)


@app.route("/api/pos/stats")
@_login_required
def api_pos_stats():
    return jsonify(pos_engine.count_tickets())


@app.route("/api/pos/products/search")
@_login_required
def api_search_products():
    q = request.args.get("q", "")
    return jsonify(pos_engine.search_products(q, limit=20))


def get_setting(key, default=""):
    try:
        return sqlite_db.get_setting(key, default)
    except Exception:
        return default


@app.route("/api/products/barcode")
@_login_required
def api_find_barcode():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "code requis"}), 400
    p = pos_engine.find_product_by_barcode(code)
    if p:
        return jsonify({"found": True, "product": p})
    return jsonify({"found": False})


# ══════════════════════════════════════════════════════════════
# HISTORIQUE DES VENTES
# ══════════════════════════════════════════════════════════════

@app.route("/sales")
@_login_required
def sales_page():
    stats = pos_engine.count_tickets()

    ticket_rows = ""
    result = pos_engine.list_tickets(limit=200)
    for t in result["tickets"]:
        vendeur_txt = "{} {}".format(t.get("vendeur_prenom", "") or "", t.get("vendeur_nom", "") or "").strip() or "-"
        ticket_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.0f}</td><td>{}</td><td>{}</td><td><a href='/pos/ticket/{}/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>".format(
            _esc(t.get("numero", "")), _esc(t.get("date_ticket", "")[:16]),
            _esc(t.get("tiers_nom", "")), t.get("montant_ttc", 0),
            _esc(t.get("mode_paiement", "")), _esc(vendeur_txt), t.get("id", "")
        )

    vendeurs = pos_engine.list_vendeurs()
    vendeur_options = '<option value="">Tous</option>'
    for v in vendeurs:
        vendeur_options += '<option value="{}">{} {}</option>'.format(v["id"], v.get("prenom", ""), v["nom"])

    body = """
<div class="card"><h2>Historique des Ventes</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value">{total}</div><div class="label">Total ventes</div></div>
<div class="stat"><div class="value">{tickets_jour}</div><div class="label">Aujourd'hui</div></div>
<div class="stat"><div class="value">{ca_jour:,.0f}</div><div class="label">CA Jour (FCFA)</div></div>
<div class="stat"><div class="value">{ca_total:,.0f}</div><div class="label">CA Total (FCFA)</div></div>
</div></div>
<div class="card">
<h2>Filtres</h2>
<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
<div><label>Date debut</label><input type="date" id="f-date-from"></div>
<div><label>Date fin</label><input type="date" id="f-date-to"></div>
<div><label>Vendeur</label><select id="f-vendeur">{vendeur_opts}</select></div>
<div><label>Recherche</label><input type="text" id="f-search" placeholder="N client..."></div>
<div><button class="btn btn-primary" onclick="filterSales()">Filtrer</button></div>
</div></div>
<div class="card"><h2>Ventes ({count})</h2>
<table><thead><tr><th>Numero</th><th>Date</th><th>Client</th><th style="text-align:right">Montant</th><th>Paiement</th><th>Vendeur</th><th></th></tr></thead>
<tbody id="sales-body">{rows}</tbody></table></div>
<script>
function filterSales(){{
  var df=document.getElementById("f-date-from").value;
  var dt=document.getElementById("f-date-to").value;
  var vid=document.getElementById("f-vendeur").value;
  var q=document.getElementById("f-search").value;
  var url="/api/pos/tickets?limit=200";
  if(df)url+="&date_from="+df;
  if(dt)url+="&date_to="+dt;
  if(vid)url+="&vendeur_id="+vid;
  if(q)url+="&search="+encodeURIComponent(q);
  api("GET",url).then(function(d){{
    var html="";
    (d.tickets||[]).forEach(function(t){{
      var v=(t.vendeur_prenom||"")+" "+(t.vendeur_nom||"");
      html+="<tr><td>"+t.numero+"</td><td>"+(t.date_ticket||"").substring(0,16)+"</td><td>"+t.tiers_nom+"</td><td style='text-align:right'>"+Number(t.montant_ttc).toLocaleString()+"</td><td>"+t.mode_paiement+"</td><td>"+(v.trim()||"-")+"</td><td><a href='/pos/ticket/"+t.id+"/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>"
    }});
    if(!html)html='<tr><td colspan="7' style="text-align:center;color:#64748b">Aucune vente</td></tr>';
    document.getElementById("sales-body").innerHTML=html;
  }})
}}
</script></div>""".format(
        total=stats.get("total", 0), tickets_jour=stats.get("tickets_jour", 0),
        ca_jour=stats.get("ca_jour", 0), ca_total=stats.get("ca_total", 0),
        count=result.get("total", 0), rows=ticket_rows,
        vendeur_opts=vendeur_options
    )
    return _page(body)


# ══════════════════════════════════════════════════════════════
# GESTION DES VENDEURS
# ══════════════════════════════════════════════════════════════

@app.route("/vendeurs")
@_login_required
def vendeurs_page():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    stats = pos_engine.get_vendeur_stats()

    rows = ""
    for v in vendeurs:
        s = "badge-ok" if v.get("est_actif") else "badge-err"
        stxt = "Actif" if v.get("est_actif") else "Inactif"
        vstat = next((x for x in stats if x["id"] == v["id"]), None)
        nb_ventes = vstat["nb_ventes"] if vstat else 0
        ca = vstat["ca_total"] if vstat else 0
        rows += "<tr><td>{}</td><td>{} {}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td><span class='badge {}'>{}</span></td><td><button class='btn btn-sm' onclick='editVendeur({})'>Modifier</button></td></tr>".format(
            _esc(v.get("code", "")), _esc(v.get("prenom", "")), _esc(v.get("nom", "")),
            _esc(v.get("role", "")), _esc(v.get("telephone", "") or v.get("email", "") or "-"),
            nb_ventes, ca, s, _esc(stxt), v["id"]
        )

    body = """
<div class="card"><h2>Equipe Vente</h2>
<div class="stat-grid" style="margin-bottom:16px">
<div class="stat"><div class="value">{total}</div><div class="label">Vendeurs</div></div>
<div class="stat"><div class="value">{actifs}</div><div class="label">Actifs</div></div>
<div class="stat"><div class="value">{ca_jour:,.0f}</div><div class="label">CA Jour equipe</div></div>
</div>
<button class="btn btn-primary" onclick="showNewVendeur()">+ Nouveau Vendeur</button>
</div>
<div class="card"><h2>Liste des Vendeurs</h2>
<table><thead><tr><th>Code</th><th>Nom</th><th>Role</th><th>Contact</th><th>Ventes</th><th style='text-align:right'>CA</th><th>Statut</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>

<div id="modal-vendeur" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;display:none;align-items:center;justify-content:center">
<div class="card" style="width:420px;max-width:95vw">
<h2 id="modal-title">Nouveau Vendeur</h2>
<form id="form-vendeur">
<input type="hidden" id="v-id">
<div class="grid-2">
<div><label>Code</label><input type="text" id="v-code" required></div>
<div><label>Nom</label><input type="text" id="v-nom" required></div>
<div><label>Prenom</label><input type="text" id="v-prenom"></div>
<div><label>Role</label><select id="v-role"><option value="vendeur">Vendeur</option><option value="vendeuse">Vendeuse</option><option value="caissier">Caissier</option><option value="responsable">Responsable</option></select></div>
<div><label>Email</label><input type="email" id="v-email"></div>
<div><label>Telephone</label><input type="text" id="v-tel"></div>
</div>
<div style="margin-top:16px">
<button type="submit" class="btn btn-primary">Enregistrer</button>
<button type="button" class="btn" onclick="closeModal()" style="margin-left:8px">Annuler</button>
</div></form></div></div>
<script>
function showNewVendeur(){{document.getElementById("modal-title").textContent="Nouveau Vendeur";document.getElementById("v-id").value="";document.getElementById("v-code").value="";document.getElementById("v-nom").value="";document.getElementById("v-prenom").value="";document.getElementById("v-role").value="vendeur";document.getElementById("v-email").value="";document.getElementById("v-tel").value="";document.getElementById("modal-vendeur").style.display="flex"}}
function editVendeur(id){{api("GET","/api/vendeurs/"+id).then(function(v){{document.getElementById("modal-title").textContent="Modifier Vendeur";document.getElementById("v-id").value=v.id;document.getElementById("v-code").value=v.code;document.getElementById("v-nom").value=v.nom;document.getElementById("v-prenom").value=v.prenom||"";document.getElementById("v-role").value=v.role||"vendeur";document.getElementById("v-email").value=v.email||"";document.getElementById("v-tel").value=v.telephone||"";document.getElementById("modal-vendeur").style.display="flex"}})}}
function closeModal(){{document.getElementById("modal-vendeur").style.display="none"}}
document.getElementById("form-vendeur").onsubmit=function(e){{e.preventDefault();var id=document.getElementById("v-id").value;var d={{code:document.getElementById("v-code").value,nom:document.getElementById("v-nom").value,prenom:document.getElementById("v-prenom").value,role:document.getElementById("v-role").value,email:document.getElementById("v-email").value,telephone:document.getElementById("v-tel").value}};var url=id?"/api/vendeurs/"+id:"/api/vendeurs";var method=id?"PUT":"POST";api(method,url,d).then(function(r){{if(r.success||r.id){{showToast("Vendeur sauvegarde","ok");setTimeout(function(){{location.reload()}},1000)}}else{{showToast("Erreur: "+(r.error||"inconnue"),"err")}}}})}}
</script>""".format(
        total=len(vendeurs),
        actifs=sum(1 for v in vendeurs if v.get("est_actif")),
        ca_jour=sum(s.get("ca_total", 0) for s in stats),
        rows=rows
    )
    return _page(body)


@app.route("/api/vendeurs", methods=["GET"])
@_login_required
def api_list_vendeurs():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    return jsonify({"vendeurs": vendeurs, "total": len(vendeurs)})


@app.route("/api/vendeurs", methods=["POST"])
@_login_required
def api_create_vendeur():
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.create_vendeur(data))


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["GET"])
@_login_required
def api_get_vendeur(vendeur_id):
    v = pos_engine.get_vendeur(vendeur_id)
    if not v:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(v)


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["PUT"])
@_login_required
def api_update_vendeur(vendeur_id):
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.update_vendeur(vendeur_id, data))


@app.route("/api/vendeurs/<int:vendeur_id>", methods=["DELETE"])
@_login_required
def api_delete_vendeur(vendeur_id):
    return jsonify(pos_engine.update_vendeur(vendeur_id, {"est_actif": 0}))


@app.route("/api/invoices/<int:invoice_id>/pdf")
@_login_required
def api_invoice_pdf(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Facture non trouvee"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_invoice_pdf(inv)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = inv.get("numero", "facture").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pos/ticket/<int:ticket_id>/pdf")
@_login_required
def api_ticket_pdf(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket non trouve"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_ticket_pdf(ticket)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = ticket.get("numero", "ticket").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contacts/<int:contact_id>", methods=["PUT"])
@_login_required
def api_update_contact(contact_id):
    data = request.get_json(silent=True) or {}
    try:
        with sqlite_db.get_cursor() as cur:
            fields = []
            vals = []
            for k in ("nom", "type", "email", "telephone", "niu", "adresse", "ville", "pays", "est_actif"):
                if k in data:
                    fields.append("{} = ?".format(k))
                    vals.append(data[k])
            if not fields:
                return jsonify({"success": False, "error": "Aucun champ a modifier"})
            fields.append("updated_at = datetime('now')")
            vals.append(contact_id)
            cur.execute("UPDATE contacts SET {} WHERE id = ?".format(", ".join(fields)), vals)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
@_login_required
def api_delete_contact(contact_id):
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/clients")
@_login_required
def clients_page():
    contacts = pos_engine.list_contacts(limit=500)
    client_count = 0
    fourni_count = 0
    for c in contacts:
        if c.get("type") == "client":
            client_count += 1
        else:
            fourni_count += 1

    rows = ""
    for c in contacts:
        s = "badge-ok" if c.get("est_actif") else "badge-err"
        stxt = "Actif" if c.get("est_actif") else "Inactif"
        typ = c.get("type", "client")
        tc = "badge-info" if typ == "fournisseur" else "badge-ok"
        niu = c.get("niu", "") or "-"
        rows += """<tr>
            <td>{code}</td><td><span class="badge {tc}">{typ}</span></td>
            <td>{nom}</td><td>{niu}</td><td>{email}</td><td>{tel}</td><td>{ville}</td>
            <td><span class="badge {s}">{stxt}</span></td>
            <td><button class="btn btn-sm btn-primary" onclick="editContact({cid})">Editer</button>
            <button class="btn btn-sm btn-danger" onclick="deleteContact({cid})">Suppr</button></td>
        </tr>""".format(
            code=c.get("code",""), tc=tc, typ=typ.upper(),
            nom=c.get("nom",""), niu=niu, email=c.get("email",""), tel=c.get("telephone",""), ville=c.get("ville",""),
            s=s, stxt=stxt,
            cid=c["id"]
        )

    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gestion Clients - T-CONNECTOR</title><style>{css}</style></head><body>
{nav}<div class="main-content">
<h2 style="margin-bottom:16px">Gestion Clients &amp; Fournisseurs</h2>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <h2 style="margin:0">Contacts ({total})</h2>
    <button class="btn btn-primary" onclick="showModal()">+ Nouveau Client</button>
  </div>
  <div style="display:flex;gap:12px;margin-bottom:12px">
    <div class="stat" style="flex:1"><div class="value">{nb_clients}</div><div class="label">Clients</div></div>
    <div class="stat" style="flex:1"><div class="value">{nb_fournis}</div><div class="label">Fournisseurs</div></div>
    <div class="stat" style="flex:1"><div class="value">{nb_total}</div><div class="label">Total</div></div>
  </div>
  <div style="margin-bottom:12px">
    <input type="text" id="searchContact" placeholder="Rechercher un contact..." oninput="filterContacts()" style="max-width:400px">
  </div>
  <table>
    <thead><tr><th>Code</th><th>Type</th><th>Nom</th><th>NIU</th><th>Email</th><th>Telephone</th><th>Ville</th><th>Statut</th><th>Actions</th></tr></thead>
    <tbody id="contactsBody">{rows}</tbody>
  </table>
</div>

<div id="contactModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;align-items:center;justify-content:center">
  <div class="card" style="width:500px;max-width:95vw">
    <h2 id="modalTitle">Nouveau Client</h2>
    <input type="hidden" id="editId">
    <div class="grid-2">
      <div><label>Code</label><input type="text" id="fCode" placeholder="CLI-XXX"></div>
      <div><label>Type</label><select id="fType"><option value="client">Client</option><option value="fournisseur">Fournisseur</option></select></div>
    </div>
    <label>Nom / Raison Sociale *</label><input type="text" id="fNom" placeholder="Nom complet">
    <div class="grid-2">
      <div><label>NIU (Identifiant fiscal)</label><input type="text" id="fNiu" placeholder="CG123456789"></div>
      <div><label>Ville</label><input type="text" id="fVille" placeholder="Pointe-Noire"></div>
    </div>
    <div class="grid-2">
      <div><label>Email</label><input type="email" id="fEmail" placeholder="contact@societe.cg"></div>
      <div><label>Telephone</label><input type="text" id="fTel" placeholder="+24206XXXXXXX"></div>
    </div>
    <label>Adresse</label><input type="text" id="fAdresse" placeholder="Quartier, rue...">
    <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
      <button class="btn" style="background:#334155;color:#94a3b8" onclick="hideModal()">Annuler</button>
      <button class="btn btn-primary" onclick="saveContact()">Enregistrer</button>
    </div>
  </div>
</div>

{toast}
<script>
var contacts = {contacts_json};

function filterContacts() {{
  var q = document.getElementById("searchContact").value.toLowerCase();
  var rows = document.getElementById("contactsBody").querySelectorAll("tr");
  for(var i=0;i<rows.length;i++){{
    var txt = rows[i].textContent.toLowerCase();
    rows[i].style.display = txt.indexOf(q)>=0 ? "" : "none";
  }}
}}

function showModal(contact) {{
  var m = document.getElementById("contactModal");
  m.style.display = "flex";
  if(contact) {{
    document.getElementById("modalTitle").textContent = "Editer Contact";
    document.getElementById("editId").value = contact.id;
    document.getElementById("fCode").value = contact.code || "";
    document.getElementById("fType").value = contact.type || "client";
    document.getElementById("fNom").value = contact.nom || "";
    document.getElementById("fNiu").value = contact.niu || "";
    document.getElementById("fVille").value = contact.ville || "";
    document.getElementById("fEmail").value = contact.email || "";
    document.getElementById("fTel").value = contact.telephone || "";
    document.getElementById("fAdresse").value = contact.adresse || "";
  }} else {{
    document.getElementById("modalTitle").textContent = "Nouveau Client";
    document.getElementById("editId").value = "";
    document.getElementById("fCode").value = "";
    document.getElementById("fNom").value = "";
    document.getElementById("fNiu").value = "";
    document.getElementById("fVille").value = "";
    document.getElementById("fEmail").value = "";
    document.getElementById("fTel").value = "";
    document.getElementById("fAdresse").value = "";
  }}
}}
function hideModal() {{ document.getElementById("contactModal").style.display = "none"; }}

function editContact(id) {{
  for(var i=0;i<contacts.length;i++){{
    if(contacts[i].id === id) {{ showModal(contacts[i]); return; }}
  }}
}}

function deleteContact(id) {{
  if(!confirm("Supprimer ce contact ?")) return;
  fetch("/api/contacts/" + id, {{method:"DELETE"}})
    .then(function(r){{return r.json()}})
    .then(function(d){{
      if(d.success) {{ showToast("Contact supprime","ok"); setTimeout(function(){{location.reload()}},800); }}
      else showToast(d.error || "Erreur","err");
    }});
}}

function saveContact() {{
  var editId = document.getElementById("editId").value;
  var data = {{
    code: document.getElementById("fCode").value,
    type: document.getElementById("fType").value,
    nom: document.getElementById("fNom").value,
    niu: document.getElementById("fNiu").value,
    ville: document.getElementById("fVille").value,
    email: document.getElementById("fEmail").value,
    telephone: document.getElementById("fTel").value,
    adresse: document.getElementById("fAdresse").value
  }};
  if(!data.nom) {{ showToast("Nom requis","err"); return; }}
  var url = "/api/contacts";
  var method = "POST";
  if(editId) {{ url = "/api/contacts/" + editId; method = "PUT"; }}
  fetch(url, {{method:method, headers:{{"Content-Type":"application/json"}}, body:JSON.stringify(data)}})
    .then(function(r){{return r.json()}})
    .then(function(d){{
      if(d.success) {{ showToast(editId ? "Contact modifie" : "Contact cree","ok"); setTimeout(function(){{location.reload()}},800); }}
      else showToast(d.error || "Erreur","err");
    }});
}}
</script>
</div></body></html>""".format(
        css=CSS, nav=_sidebar(request.path),
        total=len(contacts), nb_clients=client_count, nb_fournis=fourni_count, nb_total=len(contacts),
        rows=rows,
        contacts_json=__import__("json").dumps(contacts),
        toast=TOAST
    )
    return render_template_string(html)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ready")
def ready():
    return jsonify({"ready": True})
