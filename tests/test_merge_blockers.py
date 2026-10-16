from types import SimpleNamespace

import pytest

from maxwell.github import GitHub


def response(checks=(), *, head="head", base="base", cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": head,
                    "baseRefOid": base,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "nodes": list(checks),
                                            "pageInfo": {
                                                "hasNextPage": cursor is not None,
                                                "endCursor": cursor,
                                            },
                                        }
                                    }
                                }
                            }
                        ]
                    },
                }
            }
        }
    }


def read(*pages):
    replies = iter(pages)
    calls = []

    def request(method, path, **kwargs):
        assert (method, path) == ("POST", "graphql")
        calls.append(kwargs["json"]["variables"]["cursor"])
        return next(replies)

    result = GitHub.failed_required_checks(
        SimpleNamespace(request=request), "acme/repo", 12, "head", "base"
    )
    return result, calls


def check(name="tests", **kwargs):
    return dict(name=name, status="COMPLETED", conclusion="FAILURE", isRequired=True, **kwargs)


def test_required_check_reader_filters_and_paginates():
    failures, calls = read(
        response(
            [
                check() | {"isRequired": False},
                check() | {"status": "IN_PROGRESS"},
                check() | {"conclusion": "ACTION_REQUIRED"},
                check() | {"conclusion": "CANCELLED"},
                check() | {"conclusion": "SUCCESS"},
            ],
            cursor="next",
        ),
        response(
            [
                check("unit", databaseId=42, summary="assertion failed", text="traceback"),
                {"context": "lint", "state": "ERROR", "isRequired": True},
            ]
        ),
    )
    assert calls == [None, "next"]
    assert [failure["name"] for failure in failures] == ["lint", "unit"]
    assert failures[1]["summary"] == "assertion failed"
    assert failures[1]["text"] == "traceback"


@pytest.mark.parametrize("changed", [{"head": "new"}, {"base": "new"}])
def test_required_check_reader_refuses_revision_changes_between_pages(changed):
    with pytest.raises(ValueError, match="PR moved"):
        read(response([check()], cursor="next"), response(**changed))


@pytest.mark.parametrize(
    "page, message",
    [
        ({"errors": [{"message": "Resource not accessible"}]}, "Resource not accessible"),
        ({"data": {"repository": None}}, "incomplete required-check evidence"),
    ],
)
def test_required_check_reader_reports_unavailable_evidence(page, message):
    with pytest.raises(ValueError, match=message):
        read(page)


def test_required_check_reader_refuses_stuck_pagination():
    with pytest.raises(ValueError, match="pagination did not advance"):
        read(response(cursor="same"), response(cursor="same"))


def test_required_check_reader_bounds_output_and_accepts_absent_ci():
    failures, _ = read(
        response([check(str(index), summary="s" * 1000, text="t" * 5000) for index in range(25)])
    )
    assert len(failures) == 20
    assert all(len(failure["summary"]) == 800 for failure in failures)
    assert all(len(failure["text"]) == 2000 for failure in failures)
    empty = response()
    empty["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
        "statusCheckRollup"
    ] = None
    assert read(empty)[0] == []
