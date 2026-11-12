from app.domain.project_token import (
    SUBJECT_TOKEN_RE,
    generate_project_token,
    is_valid_project_token,
)


def test_generated_token_has_expected_shape():
    token = generate_project_token()
    assert is_valid_project_token(token)
    assert len(token) == 10


def test_tokens_are_not_constant():
    assert len({generate_project_token() for _ in range(50)}) > 40


def test_invalid_tokens_rejected():
    for bad in ["PRJ-12345", "prj-ABCDEF", "PRJ-ABCDEFG", "XYZ-ABCDEF", ""]:
        assert not is_valid_project_token(bad)


def test_subject_regex_finds_all_tokens():
    subject = "Re: [PRJ-7F3K2Q] Portal - SRS v0.3 for review [PRJ-AAAAAA]"
    assert SUBJECT_TOKEN_RE.findall(subject) == ["PRJ-7F3K2Q", "PRJ-AAAAAA"]
