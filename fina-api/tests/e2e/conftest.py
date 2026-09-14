from collections.abc import Iterator

import pytest

from tests.e2e.stack import ComposeStack


@pytest.fixture(scope="session")
def e2e_stack(request: pytest.FixtureRequest) -> Iterator[ComposeStack]:
    if not request.config.getoption("--run-e2e"):
        pytest.skip("Container E2E tests require --run-e2e and a running Docker daemon")
    stack = ComposeStack()
    try:
        try:
            stack.start()
        except Exception:
            try:
                print(stack.logs())
            except Exception as error:
                print(f"Could not collect container logs: {error}")
            raise
        yield stack
    finally:
        stack.close()


@pytest.fixture
def clean_stack(e2e_stack: ComposeStack) -> ComposeStack:
    e2e_stack.reset_database()
    return e2e_stack


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    stack = item.funcargs.get("e2e_stack")
    if report.failed and stack is not None:
        try:
            report.sections.append(("E2E container logs", stack.logs()))
        except Exception as error:
            report.sections.append(("E2E log collection", str(error)))
