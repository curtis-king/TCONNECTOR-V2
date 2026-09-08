#!/usr/bin/env python3
import random
import sqlite_db

FAMILLES = [
    "Electroménager", "Informatique", "Téléphonie", "Bijouterie",
    "Quincaillerie", "Alimentation", "Textile", "Cosmétique",
    "Construction", "Fournitures Bureau"
]

FAMILLE_PRODUCTS = {
    "Electroménager": [
        ("Ventilateur mural 30cm", "VEN-MUR-001", 18500, 12000),
        ("Ventilateur sur pied 40cm", "VEN-PED-002", 25000, 16000),
        ("Fer à repasser vapeur", "FER-VAP-003", 32000, 21000),
        ("Mixeur 3 vitesses", "MIX-3V-004", 28000, 18000),
        ("Grille-pain 2 tranches", "GRI-2T-005", 22000, 14500),
        ("Marmite électrique 3L", "MAR-3L-006", 35000, 23000),
        ("Réfrigérateur 120L", "REF-120-007", 185000, 120000),
        ("Climatiseur split 9000BTU", "CLI-9K-008", 280000, 185000),
        ("Aspirateur 1200W", "ASI-12W-009", 45000, 29000),
        ("Machine à laver 7kg", "MAL-7K-010", 220000, 145000),
    ],
    "Informatique": [
        ("Clavier USB AZERTY", "CLV-AZR-011", 8500, 5500),
        ("Souris optique sans fil", "SOU-SF-012", 12000, 7500),
        ("Écran LCD 19 pouces", "ECR-19-013", 125000, 82000),
        ("Disque dur externe 1To", "HDD-1T-014", 65000, 42000),
        ("Clé USB 64Go", "USB-64-015", 8000, 4500),
        ("Imprimante HP LaserJet", "IMP-HP-016", 185000, 120000),
        ("Casque audio filaire", "CAS-FIL-017", 15000, 9000),
        ("Haut-parleur Bluetooth", "HP-BT-018", 22000, 14000),
        ("Routeur WiFi N300", "RTR-N300-019", 28000, 18000),
        ("Onglette souris (lot 3)", "OND-LOT-020", 5000, 2500),
    ],
    "Téléphonie": [
        ("Smartphone Android 6.5\"", "TEL-AND-021", 125000, 80000),
        ("Téléphone fixes sans fil", "TEL-FIX-022", 35000, 22000),
        ("Coque silicone universelle", "COQ-SIL-023", 3500, 1500),
        ("Chargeur rapide USB-C", "CHG-UC-024", 8000, 4500),
        ("Batterie externe 10000mAh", "BAT-10K-025", 15000, 9000),
        ("Écouteur Bluetooth TWS", "ECO-TWS-026", 12000, 7000),
        ("Support téléphone vélo", "SUP-VEL-027", 6000, 3500),
        ("Vitre trempée universelle", "VTR-TMP-028", 4500, 2000),
        ("Câble micro-USB 1m", "CBL-MU-029", 2500, 1000),
        ("Kit main libre Bluetooth", "KIT-HF-030", 18000, 11000),
    ],
    "Bijouterie": [
        ("Bracelet argent 925", "BRC-ARG-031", 45000, 28000),
        ("Bague dorée femme", "BAG-DR-032", 65000, 40000),
        ("Collier perles fines", "COL-PRF-033", 38000, 24000),
        ("Montre homme analogique", "MNT-HOM-034", 85000, 55000),
        ("Boucles d'oreilles plaqué or", "BOU-PLQ-035", 25000, 15000),
        ("Pendentif coeur argent", "PEN-COE-036", 30000, 19000),
        ("Alliance mariage inox", "ALL-MRG-037", 42000, 27000),
        ("Bracelet cuir tressé", "BRC-CUI-038", 15000, 8500),
    ],
    "Quincaillerie": [
        ("Perceuse visseuse 12V", "PER-12V-039", 55000, 35000),
        ("Kit tournevis 30 pcs", "KIT-TVR-040", 18000, 11000),
        ("Mètre ruban 5m", "MET-RUB-041", 4500, 2500),
        ("Scie hacksaw métaux", "SCI-HAK-042", 12000, 7500),
        ("Pince multiprise 8\"", "PLC-MUL-043", 9500, 6000),
        ("Vis inox M5 (lot 100)", "VIS-M5-044", 6000, 3500),
        ("Tuyau PVC 32mm 3m", "TYU-PVC-045", 5500, 3000),
        ("Joint fileté 1/2\"", "JNT-FIL-046", 1200, 500),
        ("Peinture blanche 5L", "PEI-BLC-047", 32000, 20000),
        ("Marteau 500g", "MAR-500-048", 8500, 5000),
    ],
    "Alimentation": [
        ("Huile végétale 5L", "HUI-5L-049", 12000, 8500),
        ("Sucre granulé 2kg", "SUC-2K-050", 4500, 2800),
    ],
    "Textile": [
        ("T-shirt coton homme", "TSH-COT-051", 12000, 6500),
        ("Jean slim homme", "JEA-SLM-052", 25000, 14000),
    ],
    "Cosmétique": [
        ("Crème hydratante 200ml", "CRM-HYD-053", 8500, 4500),
        ("Parfum spray 100ml", "PRF-SPR-054", 35000, 20000),
    ],
    "Construction": [
        ("Ciment Portland 50kg", "CIM-PTL-055", 7500, 5000),
        ("Brique creuse standard", "BRI-CRE-056", 450, 250),
    ],
    "Fournitures Bureau": [
        ("Ramette papier A4 (500 feuilles)", "PAP-A4-057", 3500, 2000),
        ("Stylo bille bleu (lot 10)", "STY-BIL-058", 2500, 1200),
        ("Classeur à levier A4", "CLA-LEV-059", 4000, 2200),
        ("Enveloppe C4 lot 50", "ENV-C4-060", 5500, 3000),
    ],
}

CLIENTS = [
    {"code": "CLI-001", "nom": "Entreprise OBAMA", "type": "client", "email": "obama@societe.cg", "telephone": "+242066123456", "niu": "CG123456789", "adresse": "Boulevard du 15 Août", "ville": "Pointe-Noire", "pays": "CG"},
    {"code": "CLI-002", "nom": "SARL PETRO-CONGO", "type": "client", "email": "contact@petrocongo.cg", "telephone": "+242055987654", "niu": "CG987654321", "adresse": "Avenue des Nations", "ville": "Brazzaville", "pays": "CG"},
    {"code": "CLI-003", "nom": "SOCIAL ASSURANCE", "type": "client", "email": "info@sociale.cg", "telephone": "+242064456789", "niu": "CG456789012", "adresse": "Quartier Texas", "ville": "Pointe-Noire", "pays": "CG"},
    {"code": "CLI-004", "nom": "HOTEL MERIDIAN", "type": "client", "email": "reserv@meridian.cg", "telephone": "+242063321456", "niu": "CG321654987", "adresse": "Avenue de la Plage", "ville": "Pointe-Noire", "pays": "CG"},
    {"code": "CLI-005", "nom": "CARREFOUR MARKET", "type": "client", "email": "direction@carrefour.cg", "telephone": "+242062298765", "niu": "CG789012345", "adresse": "Boulevard Congo", "ville": "Brazzaville", "pays": "CG"},
    {"code": "CLI-006", "nom": "SNC MABANGA", "type": "client", "email": "mabanga@snc.cg", "telephone": "+242051123789", "niu": "CG654321098", "adresse": "Rue Kintele", "ville": "Brazzaville", "pays": "CG"},
    {"code": "CLI-007", "nom": "TECHNO STORE", "type": "client", "email": "vente@technostore.cg", "telephone": "+242066876543", "niu": "CG234567890", "adresse": "Marché Total", "ville": "Dolisie", "pays": "CG"},
    {"code": "CLI-008", "nom": "PHARMACIE CENTRALE", "type": "client", "email": "pharma@centrale.cg", "telephone": "+242055345678", "niu": "CG890123456", "adresse": "Centre-ville", "ville": "Brazzaville", "pays": "CG"},
    {"code": "CLI-009", "nom": "ENTREPRISE KOKI", "type": "client", "email": "koki@entreprise.cg", "telephone": "+242064654321", "niu": "CG567890123", "adresse": "Zone industrielle", "ville": "Pointe-Noire", "pays": "CG"},
    {"code": "CLI-010", "nom": "SUPERMARCHÉ LUX", "type": "client", "email": "achat@luxcg.com", "telephone": "+242063987123", "niu": "CG345678901", "adresse": "Boulevard Santos", "ville": "Brazzaville", "pays": "CG"},
    {"code": "FOU-001", "nom": "DISTRIBUTOR AFRIQUE", "type": "fournisseur", "email": "ventes@distafrique.cg", "telephone": "+242065543210", "niu": "CG111222333", "adresse": "Zone franche", "ville": "Pointe-Noire", "pays": "CG"},
    {"code": "FOU-002", "nom": "IMPORT-EXPORT BRAZZA", "type": "fournisseur", "email": "import@iebrazza.cg", "telephone": "+242056677889", "niu": "CG444555666", "adresse": "Marché Mikalou", "ville": "Brazzaville", "pays": "CG"},
    {"code": "FOU-003", "nom": "GROSSISTE GENERAL", "type": "fournisseur", "email": "info@grossegen.cg", "telephone": "+242067890123", "niu": "CG777888999", "adresse": "Quartier Kinkole", "ville": "Brazzaville", "pays": "CG"},
    {"code": "CLI-CPT", "nom": "Client comptoir", "type": "client", "email": "", "telephone": "", "niu": "", "adresse": "Comptoir", "ville": "", "pays": "CG"},
]


def seed_products():
    count = 0
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM products")
        existing = cur.fetchone()["c"]
        if existing >= 50:
            print("Produits existants: {} (deja genere)".format(existing))
            return existing

        for famille, products in FAMILLE_PRODUCTS.items():
            for designation, ref, prix_vente, prix_achat in products:
                barcode = "".join([str(random.randint(0, 9)) for _ in range(13)])
                tva_code = random.choice(["18", "18", "18", "5", "0"])
                stock = random.randint(5, 200)
                unite = random.choice(["U", "U", "U", "KG", "M", "L"])
                cur.execute("""
                    INSERT OR IGNORE INTO products
                    (ref, barcode, designation, famille, prix_vente, prix_achat, tva_code, unite, stock_reel)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (ref, barcode, designation, famille, prix_vente, prix_achat, tva_code, unite, stock))
                if cur.rowcount > 0:
                    count += 1
    print("Produits generes: {}".format(count))
    return count


def seed_clients():
    count = 0
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM contacts")
        existing = cur.fetchone()["c"]
        for c in CLIENTS:
            cur.execute("""
                INSERT OR IGNORE INTO contacts (code, nom, type, email, telephone, niu, adresse, ville, pays)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (c["code"], c["nom"], c["type"], c["email"], c["telephone"], c["niu"], c["adresse"], c["ville"], c["pays"]))
            if cur.rowcount > 0:
                count += 1
    if count:
        print("Clients/Fournisseurs generes ou mis a jour: {}".format(count))
    elif existing:
        print("Contacts existants: {} (deja genere)".format(existing))
    return count


def seed_vendeurs():
    defaults = [
        ("VEN-0001", "Admin", "admin", "admin@example.com", "responsable"),
        ("VEN-0002", "Moukonda", "Paul", "paul@moukonda.cg", "vendeur"),
        ("VEN-0003", "Ikobo", "Sarah", "sarah@ikobo.cg", "vendeur"),
    ]
    count = 0
    with sqlite_db.get_cursor() as cur:
        for code, nom, prenom, email, role in defaults:
            cur.execute("""
                INSERT OR IGNORE INTO vendeurs (code, nom, prenom, email, role)
                VALUES (?, ?, ?, ?, ?)
            """, (code, nom, prenom, email, role))
            if cur.rowcount > 0:
                count += 1
    print("Vendeurs generes: {}".format(count))
    return count


if __name__ == "__main__":
    print("=== T-CONNECTOR SEED DATA ===")
    sqlite_db.init_database()
    seed_vendeurs()
    seed_products()
    seed_clients()
    print("=== SEED TERMINE ===")
