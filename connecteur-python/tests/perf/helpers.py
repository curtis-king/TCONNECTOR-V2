"""Helpers partages des tests perf (importables sans ambiguite conftest)."""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_thresholds():
    with open(os.path.join(_HERE, "thresholds.json"), encoding="utf-8") as f:
        return json.load(f)


def assert_under(ms, limit, label=""):
    assert ms <= limit, "{}: {:.1f}ms > seuil {}ms".format(label or "duree", ms, limit)
