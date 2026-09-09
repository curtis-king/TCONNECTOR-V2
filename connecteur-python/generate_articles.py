"""Script génération d'articles — déplacé vers scripts/generate_articles.py.

Ce fichier à la racine est conservé comme raccourci d'appel :
    python generate_articles.py --count 20
Il délègue au script migré.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

runner = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "scripts",
    "generate_articles.py",
)

with open(runner, encoding="utf-8") as f:
    exec(compile(f.read(), runner, "exec"), {"__name__": "__main__", "__file__": runner})