"""Deterministic synthetic data for a histology / anatomic-pathology lab.

No real patient data, ever. Patient names exist in the DB only so that
"the model never sees them" is a real, testable property of the tool layer.

A handful of *planted scenarios* have fixed IDs so demos and evals are
reproducible; everything else is seeded random filler. Timestamps are
relative to `now`, so the lab always looks "live".
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

# Workflow order matters: a specimen only has blocks once it has been grossed.
STAGES = ["received", "grossed", "processing", "embedded", "sectioned",
          "stained", "slides_ready", "under_review", "signed_out"]
HAS_BLOCKS_FROM = STAGES.index("grossed")

SITES = {
    "biopsy": ["skin", "colon", "prostate", "breast", "stomach", "lung", "liver", "kidney"],
    "resection": ["colon", "breast", "thyroid", "uterus", "gallbladder", "appendix"],
    "cytology": ["thyroid FNA", "pleural fluid", "urine"],
}
PATHOLOGISTS = ["Dr. Amara Osei", "Dr. Ben Kowalski", "Dr. Carmen Ruiz", "Dr. Hiro Tanaka"]
TECHS = ["J. Alvarez", "M. Chen", "R. Patel", "S. Okafor"]

# stain -> instrument type that runs it
STAINS = {
    "H&E": "he_stainer", "PAS": "special_stainer", "GMS": "special_stainer",
    "AFB": "special_stainer", "Trichrome": "special_stainer",
    "Ki-67": "ihc_stainer", "ER": "ihc_stainer", "PR": "ihc_stainer", "HER2": "ihc_stainer",
    "p53": "ihc_stainer", "CK7": "ihc_stainer", "CK20": "ihc_stainer",
}

INSTRUMENTS = [
    ("TP-01", "Tissue processor 1", "tissue_processor"),
    ("TP-02", "Tissue processor 2", "tissue_processor"),
    ("HE-01", "H&E stainer", "he_stainer"),
    ("SS-01", "Special stains stainer", "special_stainer"),
    ("IHC-01", "IHC stainer 1", "ihc_stainer"),
    ("IHC-02", "IHC stainer 2", "ihc_stainer"),
    ("CS-01", "Coverslipper", "coverslipper"),
    ("MT-01", "Microtome 1", "microtome"),
    ("MT-02", "Microtome 2", "microtome"),
]

FIRST = ["Maria", "James", "Aisha", "Wei", "Olga", "Daniel", "Priya", "Tomás", "Grace", "Kenji", "Fatima", "Liam"]
LAST = ["Nguyen", "Smith", "Haddad", "Kim", "Ivanova", "Brown", "Mensah", "García", "Cohen", "Sato", "Ali", "Murphy"]


def accession(year: int, seq: int) -> str:
    return f"S{year % 100:02d}-{seq:05d}"


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="minutes")


def generate(n: int = 120, seed: int = 7, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    yy = now.year
    data: dict[str, list[dict[str, Any]]] = {k: [] for k in
        ("patients", "specimens", "stain_orders", "instruments", "qc_runs", "events")}

    # --- instruments + QC -------------------------------------------------
    for iid, name, itype in INSTRUMENTS:
        data["instruments"].append({"id": iid, "name": name, "type": itype, "status": "online",
                                    "location": "Histology", "note": None})
        for d in range(5, 0, -1):  # daily QC, all passing by default
            data["qc_runs"].append({"instrument_id": iid, "run_at": _iso(now - timedelta(days=d, hours=-7)),
                                    "result": "pass", "notes": None, "performed_by": rng.choice(TECHS)})

    def inst(iid: str) -> dict[str, Any]:
        return next(i for i in data["instruments"] if i["id"] == iid)

    # --- planted scenarios ------------------------------------------------
    # 1) IHC-01 failed this morning's QC (HER2 control too weak) -> HER2 orders stuck.
    inst("IHC-01").update(status="qc_hold", note="HER2 positive control under-stained; on hold pending re-run")
    data["qc_runs"].append({"instrument_id": "IHC-01", "run_at": _iso(now - timedelta(hours=3)),
                            "result": "fail", "notes": "HER2 3+ control stained 1+", "performed_by": "M. Chen"})
    # 2) H&E stainer down for maintenance since yesterday.
    inst("HE-01").update(status="maintenance", note="Reagent pump replacement; vendor ETA tomorrow 10:00")

    planted = [
        # The demo star: STAT breast core biopsy waiting on HER2, blocked by IHC-01 QC failure.
        dict(seq=312, type="biopsy", site="breast", priority="stat", status="stained", blocks=2,
             received=now - timedelta(hours=30), tat_hours=48, pathologist="Dr. Carmen Ruiz",
             stains=[("A1", "H&E", "complete"), ("A1", "ER", "complete"), ("A1", "PR", "complete"),
                     ("A1", "HER2", "in_progress")],
             flag=None),
        # Overdue routine colon resection, sitting in slides_ready.
        dict(seq=287, type="resection", site="colon", priority="routine", status="slides_ready", blocks=14,
             received=now - timedelta(hours=110), tat_hours=96, pathologist="Dr. Ben Kowalski",
             stains=[("A1", "H&E", "complete")], flag=None),
        # Labeling discrepancy already flagged.
        dict(seq=330, type="biopsy", site="skin", priority="routine", status="received", blocks=0,
             received=now - timedelta(hours=4), tat_hours=72, pathologist=None, stains=[],
             flag="Requisition site 'left forearm' does not match container label 'right forearm'"),
        # STAT specimen still processing (normal, not late).
        dict(seq=335, type="biopsy", site="kidney", priority="stat", status="processing", blocks=3,
             received=now - timedelta(hours=6), tat_hours=24, pathologist="Dr. Hiro Tanaka",
             stains=[], flag=None),
        # Under review with a GMS special stain pending.
        dict(seq=301, type="biopsy", site="lung", priority="routine", status="under_review", blocks=2,
             received=now - timedelta(hours=60), tat_hours=96, pathologist="Dr. Amara Osei",
             stains=[("A1", "H&E", "complete"), ("A2", "GMS", "ordered")], flag=None),
    ]
    planted_seqs = {p["seq"] for p in planted}

    # --- filler -------------------------------------------------------------
    filler = []
    seq = 200
    while len(filler) < n - len(planted):
        seq += 1
        if seq in planted_seqs:
            continue
        stype = rng.choices(list(SITES), weights=[6, 3, 1])[0]
        status = rng.choices(STAGES, weights=[2, 2, 2, 1, 1, 2, 2, 3, 8])[0]
        priority = "stat" if rng.random() < 0.12 else "routine"
        tat = 24 if priority == "stat" else rng.choice([72, 96])
        age = rng.uniform(1, tat * (1.2 if status != "signed_out" else 2.5))
        stage_idx = STAGES.index(status)
        blocks = 0 if stage_idx < HAS_BLOCKS_FROM else rng.randint(1, 4 if stype != "resection" else 16)
        stains = []
        if stage_idx >= STAGES.index("stained"):
            stains.append(("A1", "H&E", "complete"))
            if rng.random() < 0.25:
                st = rng.choice([s for s in STAINS if s != "H&E"])
                stains.append(("A1", st, "complete" if status == "signed_out" else rng.choice(["ordered", "in_progress", "complete"])))
        filler.append(dict(seq=seq, type=stype, site=rng.choice(SITES[stype]), priority=priority, status=status,
                           blocks=blocks, received=now - timedelta(hours=age), tat_hours=tat,
                           pathologist=rng.choice(PATHOLOGISTS) if stage_idx >= STAGES.index("stained") else None,
                           stains=stains, flag=None))

    # --- materialize ------------------------------------------------------------
    for i, s in enumerate(planted + filler):
        acc = accession(yy, s["seq"])
        pid = f"P{100000 + i}"
        data["patients"].append({"id": pid, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                                 "dob": f"{rng.randint(1940, 2004)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                                 "mrn": f"MRN{rng.randint(10**6, 10**7 - 1)}"})
        data["specimens"].append({
            "accession": acc, "patient_id": pid, "type": s["type"], "site": s["site"],
            "priority": s["priority"], "status": s["status"], "blocks": s["blocks"],
            "received_at": _iso(s["received"]), "due_at": _iso(s["received"] + timedelta(hours=s["tat_hours"])),
            "pathologist": s["pathologist"], "flag": s["flag"],
        })
        data["events"].append({"accession": acc, "ts": _iso(s["received"]), "event": "received", "actor": "Accessioning"})
        if s["status"] != "received":
            data["events"].append({"accession": acc, "ts": _iso(s["received"] + timedelta(hours=2)),
                                   "event": f"status -> {s['status']}", "actor": rng.choice(TECHS)})
        if s["flag"]:
            data["events"].append({"accession": acc, "ts": _iso(s["received"] + timedelta(minutes=20)),
                                   "event": f"flagged: {s['flag']}", "actor": "Accessioning"})
        for block, stain, st in s["stains"]:
            # In-progress stains are loaded on a specific instrument of the right type.
            instrument = None
            if st == "in_progress":
                options = [i for i, _, t in INSTRUMENTS if t == STAINS[stain]]
                instrument = "IHC-01" if (acc == accession(yy, 312) and stain == "HER2") else rng.choice(options)
            data["stain_orders"].append({"accession": acc, "block": block, "stain": stain, "status": st,
                                         "instrument_id": instrument,
                                         "ordered_at": _iso(s["received"] + timedelta(hours=3)),
                                         "ordered_by": s["pathologist"] or "Grossing"})
    return data
