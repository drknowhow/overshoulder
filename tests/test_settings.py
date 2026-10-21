from overshoulder import settings as st


def test_input_mode_defaults_to_borrow_and_unknown_modes_fall_back():
    assert st.Settings().input_mode == "borrow"
    assert st.Settings(input_mode="always").clamp().input_mode == "borrow"
    assert st.Settings(borrow_idle_s=0).clamp().borrow_idle_s == 1
    assert st.Settings(borrow_idle_s=9999).clamp().borrow_idle_s == 120


def test_load_or_default_survives_a_missing_or_broken_file(tmp_path):
    path = tmp_path / "settings.json"
    assert st.load_or_default(path) == st.Settings()
    path.write_text("[1, 2", encoding="utf-8")
    assert st.load_or_default(path) == st.Settings()
    st.save(st.Settings(input_mode="never", borrow_idle_s=9), path)
    got = st.load_or_default(path)
    assert (got.input_mode, got.borrow_idle_s) == ("never", 9)
