"""Plan loading and the frozen counts (V1.1 §3; T04 清单: 318 事件 / 62 B 停点 / 10 原类型事件).

The formal-data tests read the V1.1 package files read-only (mounted into the container at
V11_CV_DIR). They load and check the plan; they never send anything anywhere.
"""
import json
import shutil

import pytest

from simcore.control.config import ControlConfig
from simcore.control.plan import PlanError, load_plan
from simcore.datasets import Dataset

from .conftest import write_synthetic_dataset

FORMAL_DATASET = "sww_all5"


def cfg(**kw):
    base = dict(state_dir=None, expect_events=None, expect_b_stop_points=None, expect_soundproof=None)
    base.update(kw)
    return ControlConfig(**base)


# ------------------------------------------------------------------ synthetic
def test_mode_a_is_one_continuous_plan(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d")
    p = load_plan(Dataset(info["dir"]), info["dataset"], "A", cfg())
    assert p.speed == 240 and p.stop_points == [] and p.total_events == info["total"]
    assert p.summary()["speed_ratio"] == "1:240"


def test_mode_b_stop_points_are_the_pre_cut_segments(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d", segments=(3, 1, 2, 4))
    p = load_plan(Dataset(info["dir"]), info["dataset"], "B", cfg())
    assert p.speed == 60
    assert [sp.event_count for sp in p.stop_points] == [3, 1, 2, 4]
    assert sum(sp.event_count for sp in p.stop_points) == p.total_events == 10
    assert p.stop_points[-1].label() == "last_actual_cv_event"


def test_counts_that_do_not_match_the_contract_are_a_load_error(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d", segments=(2, 2, 2), soundproof_positions=(1, 4))
    ds = Dataset(info["dir"])
    with pytest.raises(PlanError) as e:
        load_plan(ds, info["dataset"], "A", cfg(expect_events=318))
    assert e.value.code == "E_EVENT_COUNT"
    with pytest.raises(PlanError) as e:
        load_plan(ds, info["dataset"], "B", cfg(expect_b_stop_points=62))
    assert e.value.code == "E_STOP_POINT_COUNT"
    with pytest.raises(PlanError) as e:
        load_plan(ds, info["dataset"], "A", cfg(expect_soundproof=10))
    assert e.value.code == "E_SOUNDPROOF_COUNT"


def test_explicit_known_ten_identity_list_must_match_the_plan(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d")
    ds = Dataset(info["dir"])
    sound = [s["event_id"] for s in info["steps"] if s["event_type"] == "SOUNDPROOF_COVER_MOVE_COMPLETED"]
    p = load_plan(ds, info["dataset"], "A", cfg(known_reject_event_ids=tuple(sound)))
    assert set(p.soundproof_event_ids) == set(sound)
    with pytest.raises(PlanError) as e:
        load_plan(ds, info["dataset"], "A", cfg(known_reject_event_ids=("1", "2")))
    assert e.value.code == "E_KNOWN_REJECT_IDS"


def test_missing_scenarios_are_reported_not_invented(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d")
    with pytest.raises(PlanError) as e:
        load_plan(Dataset(info["dir"]), "nosuchds", "A", cfg())
    assert e.value.code == "E_SCENARIO_NOT_FOUND"
    with pytest.raises(PlanError) as e:
        load_plan(Dataset(info["dir"]), "nosuchds", "B", cfg())
    assert e.value.code == "E_B_SEGMENTS_MISSING"


def test_a_tampered_b_segment_sequence_is_refused(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d")
    path = info["dir"] / "scenarios.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scenarios"][f"{info['dataset']}_B060_p02"]["steps"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PlanError) as e:
        load_plan(Dataset(info["dir"]), info["dataset"], "B", cfg())
    assert e.value.code == "E_SCENARIO_EMPTY"


def test_a_manifest_contradiction_is_a_load_error(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d")
    m = info["dir"] / "manifest.json"
    data = json.loads(m.read_text(encoding="utf-8"))
    data["B_segments"] = data["B_segments"][:2]
    m.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PlanError) as e:
        load_plan(Dataset(info["dir"]), info["dataset"], "B", cfg())
    assert e.value.code == "E_MANIFEST_SEGMENT_COUNT"


def test_non_monotonic_offsets_are_refused(tmp_path):
    info = write_synthetic_dataset(tmp_path / "d", offsets=[0, 5, 3, 7, 9, 11])
    with pytest.raises(PlanError) as e:
        load_plan(Dataset(info["dir"]), info["dataset"], "A", cfg())
    assert e.value.code == "E_STEP_ORDER"


# ------------------------------------------------------------------ formal V1.1 data (read-only)
@pytest.fixture
def formal_dataset(v11_cv_dir, tmp_path):
    """Copy the frozen dataset into the layout the simulator serves: data/<ds>/{scenarios,manifest}.json."""
    src = v11_cv_dir / "data" / FORMAL_DATASET
    dst = tmp_path / "formal" / FORMAL_DATASET
    dst.mkdir(parents=True)
    for name in ("scenarios.json", "catalogue.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    shutil.copy2(v11_cv_dir / "manifests" / f"{FORMAL_DATASET}.json", dst / "manifest.json")
    return dst.parent


def test_formal_plan_a_is_318_events_at_1_240(formal_dataset):
    p = load_plan(Dataset(formal_dataset / FORMAL_DATASET), FORMAL_DATASET, "A", cfg(expect_events=318, expect_soundproof=10))
    assert p.total_events == 318
    assert p.speed == 240
    assert len(p.soundproof_event_ids) == 10
    assert p.nominal_seconds == 3738.75
    assert len(set(s["event_id"] for s in p.steps)) == 318


def test_formal_plan_b_is_62_stop_points_at_1_60(formal_dataset):
    p = load_plan(
        Dataset(formal_dataset / FORMAL_DATASET),
        FORMAL_DATASET,
        "B",
        cfg(expect_events=318, expect_b_stop_points=62, expect_soundproof=10),
    )
    assert len(p.stop_points) == 62
    assert p.total_events == 318
    assert p.speed == 60
    assert p.nominal_seconds == 14955.0
    assert sum(sp.event_count for sp in p.stop_points) == 318
    assert p.stop_points[0].label() == "02-13右:S-QL-01:profile_11:1"
    assert p.stop_points[-1].label() == "last_actual_cv_event"
    assert [sp.sequence for sp in p.stop_points] == list(range(1, 63))


def test_formal_known_ten_identities(formal_dataset):
    p = load_plan(Dataset(formal_dataset / FORMAL_DATASET), FORMAL_DATASET, "A", cfg(expect_events=318, expect_soundproof=10))
    assert set(p.soundproof_event_ids) == {
        "40426882302566085",
        "40444384044017397",
        "40411400101151442",
        "40406986750516956",
        "40465694657512843",
        "40436457065051381",
        "40465103768153959",
        "40432776151154675",
        "40497106193224367",
        "40466777443697169",
    }


def test_formal_b_segments_reproduce_the_continuous_order(formal_dataset):
    """Loading mode B cross-checks the 62 segments against both continuous scenarios."""
    ds = Dataset(formal_dataset / FORMAL_DATASET)
    b = load_plan(ds, FORMAL_DATASET, "B", cfg(expect_events=318, expect_b_stop_points=62))
    a = load_plan(ds, FORMAL_DATASET, "A", cfg(expect_events=318))
    assert [s["event_id"] for s in b.steps] == [s["event_id"] for s in a.steps]
