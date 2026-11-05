from overshoulder import settings as st


def test_input_mode_defaults_to_borrow_and_unknown_modes_fall_back():
    assert st.Settings().input_mode == "borrow"
    assert st.Settings(input_mode="always").clamp().input_mode == "never"  # unknown never widens
    assert st.Settings(borrow_idle_s=0).clamp().borrow_idle_s == 1
    assert st.Settings(borrow_idle_s=9999).clamp().borrow_idle_s == 120


def test_a_missing_file_gives_the_defaults_and_a_broken_one_never_widens_what_was_set(tmp_path):
    path = tmp_path / "settings.json"
    assert st.load_or_default(path) == st.Settings() and st.problem(path) == ""
    path.write_text("[1, 2", encoding="utf-8")  # broken before anything was read from it
    got = st.load_or_default(path)
    assert got == st.restrictive() and got.input_mode == "never" and not got.waits_enabled
    assert "could not be read" in st.problem(path)
    st.save(st.Settings(input_mode="full", borrow_idle_s=9), path)
    assert st.load_or_default(path).input_mode == "full" and st.problem(path) == ""
    path.write_text("{", encoding="utf-8")  # broken after: the last settings read stay
    got = st.load_or_default(path)
    assert (got.input_mode, got.borrow_idle_s) == ("full", 9) and "last settings" in st.problem(path)


def test_a_file_with_wrong_types_is_treated_as_broken(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"borrow_idle_s": "soon", "input_mode": "full"}', encoding="utf-8")
    assert st.load_or_default(path).input_mode == "never"
