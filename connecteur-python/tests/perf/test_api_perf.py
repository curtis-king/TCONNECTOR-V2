"""Tests perf TTFB : GET pages principales via client authentifie (mocks actifs)."""
import time

import pytest

from .helpers import assert_under

pytestmark = pytest.mark.perf


def _get_ms(client, path):
    t0 = time.perf_counter()
    resp = client.get(path)
    ms = (time.perf_counter() - t0) * 1000
    assert resp.status_code == 200, "{} -> {}".format(path, resp.status_code)
    return ms


def test_billing_ttfb(client, thresholds):
    ms = _get_ms(client, "/billing")
    assert_under(ms, thresholds["ttfb_ms"]["billing"], "GET /billing")


def test_pos_ttfb(client, thresholds):
    ms = _get_ms(client, "/pos")
    assert_under(ms, thresholds["ttfb_ms"]["pos"], "GET /pos")


def test_certified_print_ttfb(client, thresholds):
    ms = _get_ms(client, "/certified/MOCK-UUID-0001/print")
    assert_under(ms, thresholds["ttfb_ms"]["certified_print"], "GET /certified/<id>/print")
