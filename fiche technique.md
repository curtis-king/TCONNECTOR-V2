# FICHE TECHNIQUE — T-CONNECTOR SFEC

## 1. Presentation Generale

**Nom du projet** : T-CONNECTOR SFEC
**Type** : Connecteur de synchronization et de certification de factures
**Technologie** : Python 3.x
**Role** : Assurer la liaison entre le logiciel de gestion Sage 100 et l'API SFEC (Systeme de Facturation Electronique Certifie) du gouvernement de la Republique du Congo.

---

## 2. Role et Fonctionnement

### Role principal
Le T-CONNECTOR est un middleware qui :
- **Lit** les donnees comptables (factures ventes, achats, contacts, taux TVA, plan comptable) depuis la base SQL Server de Sage 100
- **Envoie** automatiquement les factures de vente a l'API SFEC pour certification gouvernementale
- **Ecrit** les resultats de certification (numero, signature, QR code) directement dans les colonnes SFEC de la table Sage 100
- **Fournit** un dashboard web pour monitorer, gerer et configurer le systeme

### Fonctionnement en details

```
Sage 100 (SQL Server)  <-->  T-CONNECTOR (Python)  <-->  API SFEC (Gouvernement)
```

1. **Polling intelligent** : Le connecteur interroge Sage 100 toutes les 30s (configurable) pour detecter les nouvelles factures avec le statut "A COMPTABILISER"
2. **Auto-certification** : Les factures eligible sont envoyees automatiquement a l'API SFEC en parallele (configurable, max 5 simultanees)
3. **Gestion hors ligne** : En cas de perte internet, les factures sont mises en file d'attente et retraitées automatiquement a la reconnexion
4. **Reconciliation** : Toutes les 8 cycles, le systeme verifie la coherence entre les certifications locales et celles enregistrees sur SFEC
5. **Ecriture retour** : Les numero de certification, signatures, QR codes et dates sont ecrits dans les colonnes SFEC de la table F_DOCENTETE

---

## 3. Tableau Technique

| Composant | Technologie | Details |
|---|---|---|
| **Langage** | Python 3.x | Compatible PyInstaller (executable) |
| **Base Sage 100** | SQL Server | Connexion via pyodbc + ODBC Driver 17/13 |
| **API SFEC** | REST (HTTPS) | Authentification par cle API (X-API-Key) |
| **Dashboard** | Flask + HTML/CSS/JS | Serveur Waitress (prod) ou Flask dev |
| **Authentification session** | Flask session | Email + mot de passe, secret configurable |
| **Gestion config** | JSON (config.json) | Fusion deep-merge avec defaults |
| **Logging** | RotatingFileHandler | output.log + error.log (5 Mo, 3 rotations) |
| **Executables** | PyInstaller (.spec) | Mode portable ou service Windows |
| **Service Windows** | sc create / net start | Installation automatique via install_service.bat |

---

## 4. Modules Principaux

| Fichier | Role |
|---|---|
| `main.py` | Point d'entree, orchestration des threads, gestion des signaux |
| `database.py` | Connexion SQL Server, requetes, decouverte auto des tables, auto-migration colonnes SFEC |
| `sync_engine.py` | Moteur de polling, sync complet/incrementale, auto-certification, file d'attente retry |
| `sfec_client.py` | Client HTTP pour l'API SFEC (certifier, lister, verifier) |
| `sfec_endpoints.py` | Transformation donnees Sage -> payload SFEC, validation, certification |
| `connectivity.py` | Verification internet (DNS + HTTP), cache 10s, thread arriere-plan |
| `dashboard.py` | Dashboard web complet (7 onglets config, factures, certifiees, etc.) |
| `config_manager.py` | Gestion du fichier config.json avec defaults et deep-merge |

---

## 5. Donnees Manipulees

| Table Sage 100 | Donnees extraites |
|---|---|
| `F_DOCENTETE` | En-tetes factures (numero, date, montants, statut, tiers) |
| `F_DOCLIGNE` | Lignes de factures (designation, quantite, prix, TVA) |
| `F_COMPTET` | Tiers (clients, fournisseurs, NIU, contact) |
| `F_TVA` | Taux de TVA |
| `F_COMPTEG` | Plan comptable |
| `F_ARTICLE` | Articles (design, famille, nature) |

**Colonnes SFEC ajoutees automatiquement a F_DOCENTETE :**
- `SFEC_NUM_CERTIF` / `SFEC_NUM_CERTIF1` / `SFEC_NUM_CERTIF2`
- `SFEC_SIGNATURE`
- `SFEC_QR_CODE`
- `SFEC_DATE_CERTIF` / `SFEC_DATE_CERTIF1`
- `SFEC_STATUT` (EN_COURS / CERTIFIE / DEJA_CERTIFIE / ERREUR / A_SURVEILLER)

---

## 6. Statuts de Certification SFEC

| Statut | Signification |
|---|---|
| `(vide)` | Non encore traitee |
| `EN_COURS` | En cours de certification |
| `CERTIFIE` | Certifiee avec succes |
| `DEJA_CERTIFIE` | Deja certifiee sur SFEC (409) |
| `ERREUR` | Echec de certification |
| `A_SURVEILLER` | Certifiee mais verification echouee (502) |

---

## 7. Configuration Principale (config.json)

| Section | Parametres cles |
|---|---|
| `db` | server, database, user, password, polling_interval_ms, polling_enabled |
| `sfec` | api_key, base_url, sandbox_url, use_sandbox, enabled, certify_status |
| `company` | name, tax_number (NIU), currency, invoice_prefix |
| `queue` | max_concurrent (5), max_retries (4), retry_base_delay_ms |
| `dashboard` | port (3000), auth_enabled, login_email, login_password |
| `validation` | strict_mode, tolerance_amount |
| `rate_limit` | daily_max (2500), per_minute_max (100) |

---

## 8. Endpoints API SFEC

| Methode | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/invoices` | Certifier une facture |
| `GET` | `/api/v1/invoices` | Lister les factures certifiees |
| `GET` | `/api/v1/invoices/{id}` | Obtenir une facture par ID SFEC |

---

## 9. Fonctionnalites Specifiques

- **Decouverte automatique des tables** : Le connecteur detecte automatiquement les noms reels des tables Sage 100 (dbo_F_DOCENTETE, F_DOCENTETE, etc.)
- **Fallback SQL** : Requetes de secours en cas d'erreur sur la requete principale
- **Nettoyage des statuts bloques** : Reset automatique des factures reste en "EN_COURS" sans certification
- **Mapping automatique du type de destinataire** : business, individual, government, foreign
- **Gestion des virgules decimales** : Conversion automatique des formats francais (virgule -> point)
- **Impression des factures certifiees** : Page d'impression avec QR code et details de certification

---

## 10. Lancement et Deploiement

| Mode | Commande |
|---|---|
| Developpement | `python main.py` ou `start.bat` |
| Portable | `lancer.bat` (lance TConnector.exe) |
| Service Windows | `install_service.bat` (creer le service "TConnectorSFEC") |
| Desinstallation | `uninstall_service.bat` ou `desinstaller.bat` |

**Dashboard accessible sur** : `http://localhost:3000`
