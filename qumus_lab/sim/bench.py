"""--demo starting bench: the same objects and ids as the simulated lab (qumus-simlab, seed bench), so a
script or the brain sees the same ledger on either. Demo only: the real lab's ledger is what a person
entered (qumus-state) or what the operations recorded."""

from __future__ import annotations

DEMO_SAMPLES = (
    [("carrier_gr_01", "carrier", "left_slide_1", "available", {"material": "Gr"}),
     ("carrier_hbn_01", "carrier", "left_slide_2", "available", {"material": "hBN"})]
    + [(f"chip_{i:03d}", "chip", f"left_chip_{i}", "bare", {"substrate": "SiO2/Si 285nm"}) for i in range(1, 10)]
    + [("chip_e01", "chip", "right_chip_1", "patterned",
        {"substrate": "SiO2/Si 285nm", "electrodes": "eight existing source/drain and voltage contact tips"})]
    + [(f"stamp_{i:02d}", "stamp", f"right_stamp_{i}", "fresh", {"film": "PC"}) for i in range(1, 4)]
    + [("tape", "tape", "tape", "fresh", {"material": None, "uses": 0})]
)


def seed_demo_bench(store, *, reset: bool = False) -> int:
    """Put the demo objects in the ledger. Only into an empty ledger, unless ``reset`` (all samples and
    reservations are removed first). Returns how many samples were written."""
    rows = store.list_samples()
    if rows and not reset:
        return 0
    for s in rows:
        store.remove_sample(s["id"])
    for kind_key in list(store.reservations("slot")):
        store.release_reservation("slot", kind_key)
    for sid, kind, loc, status, props in DEMO_SAMPLES:
        store.upsert_sample(sid, kind, location=loc, status=status, props=props, location_source="manual")
    return len(DEMO_SAMPLES)
