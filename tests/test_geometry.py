import math

import numpy as np

from verify_chart import ClipIndex, capsules_for, seg_seg_dist, Records


def square(x0, y0, x1, y1):
    return [('m', (x0, y0)), ('l', (x1, y0)), ('l', (x1, y1)), ('l', (x0, y1)), ('h', ())]


def test_segment_distance():
    a = np.array([[0, 0, 10, 0], [0, 0, 10, 0], [0, 0, 10, 0]], float)
    b = np.array([[5, -1, 5, 1], [0, 3, 10, 3], [13, 4, 20, 4]], float)
    assert np.allclose(seg_seg_dist(a, b), [0, 3, 5])


def test_even_odd_ring_has_a_hole():
    ci = ClipIndex(True, [square(0, 0, 100, 100), square(25, 25, 75, 75)])
    inside = ci.inside(np.array([10.0, 50.0, 150.0]), np.array([10.0, 50.0, 50.0]))
    assert inside.tolist() == [True, False, False]


def test_nonzero_same_direction_squares_have_no_hole():
    ci = ClipIndex(False, [square(0, 0, 100, 100), square(25, 25, 75, 75)])
    assert ci.inside(np.array([50.0]), np.array([50.0])).tolist() == [True]


def test_touching_uses_capsule_radius():
    ci = ClipIndex(True, [square(0, 0, 100, 100)])
    caps = np.array([[110, 50, 130, 50, 5], [110, 50, 130, 50, 10.5], [200, 200, 200, 200, 1]], float)
    assert ci.touching(caps).tolist() == [False, True, False]


def test_capsule_contains_box():
    R = Records()
    R.box.extend((0.0, 0.0, 10.0, 2.0))
    R.seg.extend((math.nan,) * 5)
    cap = capsules_for(R, np.array([0]))[0]
    for x, y in [(0, 0), (10, 2), (0, 2), (10, 0), (5, 1)]:
        d = seg_seg_dist(np.array([[x, y, x, y]], float), cap[None, :4])[0]
        assert d <= cap[4] + 1e-9
