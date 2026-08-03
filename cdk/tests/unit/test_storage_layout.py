from cdk.storage_stack import OPENZFS_MAX_GIB, OPENZFS_MIN_GIB, openzfs_volume_layout


def test_openzfs_volume_layout_fits_minimum_parent_capacity():
    layout = openzfs_volume_layout(OPENZFS_MIN_GIB)

    assert layout == {
        "tools": (64, 16),
        "work": (64, 32),
        "scratch": (64, 0),
    }


def test_openzfs_volume_layout_stays_within_parent_capacity():
    for parent_size in (OPENZFS_MIN_GIB, 128, 320, 10_240, OPENZFS_MAX_GIB):
        layout = openzfs_volume_layout(parent_size)

        assert all(quota <= parent_size for quota, _ in layout.values())
        assert all(reservation <= quota for quota, reservation in layout.values())
        assert sum(reservation for _, reservation in layout.values()) <= parent_size
