"""Konfigurimi: mapping i statuseve/llojeve legacy → kanonike (spec §12.3)."""
import config


def test_every_legacy_status_maps_to_canonical():
    for legacy, canonical in config.LEGACY_STATUS_MAP.items():
        assert canonical in config.ALL_STATUSES, f"{legacy} → {canonical} jo kanonike"


def test_every_canonical_status_has_label():
    for s in config.ALL_STATUSES:
        assert s in config.STATUS_LABELS, f"pa etiketë: {s}"
        assert config.STATUS_LABELS[s].strip()


def test_status_sets_cover_only_canonical():
    for kind, lst in config.STATUS_SETS.items():
        for s in lst:
            assert s in config.ALL_STATUSES


def test_canon_status_passthrough_and_legacy():
    assert config.canon_status("new") == "new"
    assert config.canon_status("e re") == "new"
    assert config.canon_status("humbur") == "lost"
    assert config.canon_status("") == ""
    assert config.canon_status("i-pangjarë") == "i-pangjarë"  # i panjohur kthehet siç është


def test_canon_kind_legacy_opportunity():
    assert config.canon_kind("opportunity") == "freelance"
    assert config.canon_kind("job") == "job"
    assert config.canon_kind("product_lead") == "product_lead"
    assert config.canon_kind("local_job") == "local_job"


def test_all_kinds_have_quick_actions_and_status_set():
    for kind in config.KINDS:
        assert kind in config.QUICK_ACTIONS
        assert kind in config.STATUS_SETS
